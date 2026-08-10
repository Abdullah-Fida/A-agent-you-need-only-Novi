"""
Multi-Key OpenRouter AI Engine with automatic failover, retry, and self-healing.
Routes different tasks to different free models via a pool of API keys.
"""
import asyncio
import logging
import random
import re
from typing import List, Optional, Dict
from openai import AsyncOpenAI

logger = logging.getLogger("OmniBot.AI")

# Model assignments for different tasks.
# "openrouter/free" is an auto-router that picks an available free model.
MODELS = {
    "synthesizer":      "openrouter/free",  # social posts (Telegram/X/Reddit)
    "stealth":          "openrouter/free",  # human-sounding group replies
    "signal_cleansing": "openrouter/free",  # crypto signal rewriting
    "article":          "openrouter/free",  # long-form website articles
    "seo":              "openrouter/free",  # meta title/description/keywords
    "social_caption":   "openrouter/free",  # Facebook / Buffer captions
}

# Fallback chain used when a model returns 404 / "not a valid model ID".
# Model availability on OpenRouter's free tier changes over time, so we try
# several rather than hardcoding one that can silently rot.
FALLBACK_MODELS = [
    "openrouter/free",
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "inclusionai/ling-3.0-tiny:free",
]

DEFAULT_MODEL = "openrouter/free"


def _apply_model_overrides():
    """
    Lets any task be pointed at a different model without touching code:

        MODEL_ARTICLE=deepseek/deepseek-r1
        MODEL_SYNTHESIZER=openai/gpt-4o-mini

    This is the seam for swapping in a dedicated article-writing agent later.
    """
    import os
    for task in list(MODELS):
        override = os.environ.get(f"MODEL_{task.upper()}", "").strip()
        if override:
            MODELS[task] = override
            logger.info(f"Model override: task '{task}' -> {override}")


_apply_model_overrides()


class AIEngine:
    """
    Multi-key AI engine with automatic failover.
    If a key fails or is rate-limited, it rotates to the next one.
    If all keys fail, it logs a critical alert to Supabase.
    """
    
    # Known OpenAI-compatible providers. Both OpenRouter and Groq speak the
    # same wire protocol, so one client class serves either.
    PROVIDERS = {
        "openrouter": "https://openrouter.ai/api/v1",
        "groq":       "https://api.groq.com/openai/v1",
        "openai":     "https://api.openai.com/v1",
    }

    def __init__(self, api_keys: List[str], db=None, base_url: str = "",
                 provider: str = "openrouter", default_model: str = "",
                 label: str = "AI"):
        """
        Args:
            api_keys:      one or more keys, rotated on failure
            base_url:      explicit endpoint; overrides `provider`
            provider:      'openrouter' | 'groq' | 'openai'
            default_model: model used when a task has no specific mapping
            label:         name used in logs, e.g. 'ArticleAI'
        """
        if not api_keys:
            raise ValueError(f"{label}: at least one API key is required.")

        self.api_keys = api_keys
        self.current_key_index = 0
        self.db = db
        self.label = label
        self.provider = (provider or "openrouter").lower()
        self.base_url = base_url or self.PROVIDERS.get(self.provider, self.PROVIDERS["openrouter"])
        self.default_model = default_model or DEFAULT_MODEL

        self._build_client()
        logger.info(f"{label} initialized — provider={self.provider}, "
                    f"model={self.default_model}, keys={len(api_keys)}")

    def _build_client(self):
        """Builds an AsyncOpenAI client using the current API key."""
        self.client = AsyncOpenAI(
            api_key=self.api_keys[self.current_key_index],
            base_url=self.base_url,
        )
        logger.info(f"{self.label}: using API key index {self.current_key_index} "
                    f"({self.api_keys[self.current_key_index][:12]}...)")
    
    def _rotate_key(self) -> bool:
        """
        Rotates to the next API key.

        Returns False when we have wrapped back to the first key (i.e. every
        key has now been tried once). With a single key configured this always
        returns False, so callers must not treat it as "give up immediately" —
        the retry budget in generate() governs that instead.
        """
        if len(self.api_keys) <= 1:
            return False

        self.current_key_index += 1
        if self.current_key_index >= len(self.api_keys):
            self.current_key_index = 0  # Reset to first key
            self._build_client()
            return False  # All keys have been tried
        self._build_client()
        logger.warning(f"Rotated to API key index {self.current_key_index}.")
        return True
    
    async def generate(self, task: str, system_prompt: str, user_prompt: str, 
                       max_tokens: int = 500, temperature: float = 0.7) -> Optional[str]:
        """
        Generates AI text with automatic key rotation on failure.
        
        Args:
            task: The task type key (e.g., 'synthesizer', 'headline', 'stealth')
            system_prompt: The system instruction for the AI
            user_prompt: The user-facing prompt
            max_tokens: Maximum response length
            temperature: Creativity level (0.0 = factual, 1.0 = creative)
        
        Returns:
            The generated text, or None if all keys failed.
        """
        # A dedicated engine (e.g. the article agent on its own key) uses its
        # configured model; the shared engine routes per task.
        if self.default_model != DEFAULT_MODEL:
            model = self.default_model
        else:
            model = MODELS.get(task, self.default_model)

        # Always give at least 3 tries even with a single key, and back off
        # between them so a transient 429 doesn't kill the whole post.
        max_attempts = max(3, len(self.api_keys) * 2)
        tried_models = {model}
        last_error = ""

        for attempt in range(max_attempts):
            try:
                response = await self.client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    max_tokens=max_tokens,
                    temperature=temperature
                )

                content = response.choices[0].message.content
                if not content or not content.strip():
                    logger.warning(f"AI returned empty content on attempt {attempt + 1}. Retrying...")
                    self._rotate_key()
                    await self._backoff(attempt)
                    continue

                # Quality gate: reject if AI echoed back instructions.
                # Only check the opening of the response — these phrases can
                # legitimately appear inside a long article body.
                head = content[:200].lower()
                if "your task is to" in head or "your job is" in head:
                    logger.warning("AI returned instructions back instead of content. Retrying...")
                    self._rotate_key()
                    await self._backoff(attempt)
                    continue

                return content.strip()

            except Exception as e:
                last_error = str(e)
                logger.error(f"AI generation failed (attempt {attempt + 1}/{max_attempts}): {last_error}")
                lowered = last_error.lower()

                if "429" in last_error or "rate" in lowered:
                    logger.warning("Rate limited. Rotating key and backing off...")
                    self._rotate_key()
                elif "401" in last_error or "403" in last_error:
                    logger.warning("Authentication failed. Rotating API key...")
                    self._rotate_key()
                elif "404" in last_error or "not a valid model" in lowered or "unavailable" in lowered:
                    # Walk the fallback chain instead of a single hardcoded model.
                    # A dedicated engine has no fallback list — its model is the
                    # whole point — so it just retries.
                    chain = FALLBACK_MODELS if self.default_model == DEFAULT_MODEL else []
                    nxt = next((m for m in chain if m not in tried_models), None)
                    if nxt:
                        logger.warning(f"Model '{model}' unavailable. Falling back to '{nxt}'.")
                        model = nxt
                        tried_models.add(nxt)
                    else:
                        logger.error("All fallback models exhausted.")
                else:
                    self._rotate_key()

                await self._backoff(attempt)

        logger.critical(f"AI generation failed after {max_attempts} attempts. Last error: {last_error}")
        if self.db:
            await self.db.log_alert(
                level="CRITICAL",
                module="AIEngine",
                message=f"AI generation failed after {max_attempts} attempts "
                        f"across {len(self.api_keys)} key(s). Last error: {last_error[:400]}"
            )
        return None

    @staticmethod
    async def _backoff(attempt: int):
        """Exponential backoff with jitter, capped so we never stall a post slot."""
        delay = min(2 ** attempt, 20) + random.uniform(0, 1.5)
        await asyncio.sleep(delay)
    
    async def synthesize_news(self, raw_articles: List[Dict], niche_context: str) -> Optional[Dict]:
        """
        Takes 2-3 raw news articles about the same story and synthesizes them
        into a Novi News post with Pakistani/South Asian lens.
        
        Returns a dict with 'telegram_text' and 'tweet_text' keys.
        """
        # Build the source material
        sources_text = ""
        source_credits = []
        for i, article in enumerate(raw_articles, 1):
            sources_text += f"\n--- Source {i}: {article.get('source', 'Unknown')} ---\n"
            sources_text += f"Title: {article.get('title', '')}\n"
            sources_text += f"Summary: {article.get('summary', '')}\n"
            source_credits.append(article.get('source', 'Unknown'))
        
        credit_line = ", ".join(set(source_credits))
        
        system_prompt = """You are a senior news editor for "Novi News" — a Telegram channel 
that delivers International News, Crypto/Web3, Business, Tech & Pakistani news to a global audience.

Your writing style:
- Write strictly in 100% professional, flawless English. Do NOT use any Urdu words or phrases.
- Professional but conversational tone — like a smart friend explaining news over coffee.
- For crypto news: Focus on market impact, price action, regulatory changes, and investor insights.
- For Pakistani news: Explain WHY this matters for Pakistan, the rupee, or South Asian economies.
- For international news: Explain the global significance and broader implications.
- Use a lot of relevant emojis heavily to make the post highly visual, engaging, and fun (5+ emojis per post). Prioritize highly animated Telegram emojis like 🔥, 🚀, 📈, 💡, ⚡, 🚨, 💸, ⚠️.
- NEVER copy-paste from sources. Synthesize in your own words.
- Keep posts to 3-5 lines maximum"""

        user_prompt = f"""Read the following news sources. Your task is to output the final written content ONLY. DO NOT output your thought process. DO NOT repeat these instructions back to me. Output ONLY the raw final posts matching the requested formatting below.

1. A TELEGRAM POST (3-5 lines, strictly in English, with global/crypto/Pakistani lens as appropriate, lots of emojis)
2. A TWEET (max 280 chars, strictly in English, punchy, with 1-2 hashtags)
3. A REDDIT POST with a title and body (strictly in English, informative, neutral tone)

Format your response EXACTLY like this (do not include anything outside of these tags):
---TELEGRAM---
[write telegram post here]
---TWEET---
[write tweet here]
---REDDIT_TITLE---
[write reddit title]
---REDDIT_BODY---
[write reddit body]
---END---

Context: {niche_context}

Sources:
{sources_text}"""


        result = await self.generate(
            task="synthesizer",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=1200,
            temperature=0.7
        )
        
        if not result:
            return None
        
        # Parse the structured response
        telegram_text = ""
        tweet_text = ""
        reddit_title = ""
        reddit_body = ""
        
        try:
            # Safer parsing with regex or basic string splitting that doesn't crash on missing markers
            import re
            
            # Extract Telegram
            tg_match = re.search(r'---TELEGRAM---(.*?)(?:---TWEET---|---REDDIT_TITLE---|---END---|$)', result, re.DOTALL)
            if tg_match:
                telegram_text = tg_match.group(1).strip()
            else:
                telegram_text = result.strip()
                
            # Extract Tweet
            tw_match = re.search(r'---TWEET---(.*?)(?:---REDDIT_TITLE---|---REDDIT_BODY---|---END---|$)', result, re.DOTALL)
            if tw_match:
                tweet_text = tw_match.group(1).strip()
            else:
                # If no tweet, generate a dummy tweet from telegram
                tweet_text = telegram_text[:270] + "..." if len(telegram_text) > 270 else telegram_text
                
            # Extract Reddit
            rt_match = re.search(r'---REDDIT_TITLE---(.*?)(?:---REDDIT_BODY---|---END---|$)', result, re.DOTALL)
            if rt_match:
                reddit_title = rt_match.group(1).strip()
            
            rb_match = re.search(r'---REDDIT_BODY---(.*?)(?:---END---|$)', result, re.DOTALL)
            if rb_match:
                reddit_body = rb_match.group(1).strip()
                
        except Exception as e:
            logger.error(f"Regex parsing failed: {e}")
            telegram_text = result.strip()
            tweet_text = telegram_text[:270] + "..."
        
        return {
            "telegram_text": telegram_text,
            "tweet_text": tweet_text,
            "reddit_title": reddit_title,
            "reddit_body": reddit_body,
            "source_credits": credit_line
        }
    
