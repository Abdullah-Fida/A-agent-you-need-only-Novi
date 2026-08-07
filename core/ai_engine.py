"""
Multi-Key OpenRouter AI Engine with automatic failover, retry, and self-healing.
Routes different tasks to different free models via a pool of API keys.
"""
import logging
import re
from typing import List, Optional, Dict
from openai import AsyncOpenAI

logger = logging.getLogger("OmniBot.AI")

# Model assignments for different tasks
MODELS = {
    "synthesizer": "openrouter/free",
    "headline":    "openrouter/free",
    "stealth":     "openrouter/free",
}


class AIEngine:
    """
    Multi-key AI engine with automatic failover.
    If a key fails or is rate-limited, it rotates to the next one.
    If all keys fail, it logs a critical alert to Supabase.
    """
    
    def __init__(self, api_keys: List[str], db=None):
        if not api_keys:
            raise ValueError("At least one OpenRouter API key is required.")
        
        self.api_keys = api_keys
        self.current_key_index = 0
        self.db = db  # Reference to SupabaseDB for logging alerts
        self._build_client()
        logger.info(f"AI Engine initialized with {len(api_keys)} API key(s).")
    
    def _build_client(self):
        """Builds an AsyncOpenAI client using the current API key."""
        self.client = AsyncOpenAI(
            api_key=self.api_keys[self.current_key_index],
            base_url="https://openrouter.ai/api/v1"
        )
        logger.info(f"Using API key index {self.current_key_index} "
                     f"({self.api_keys[self.current_key_index][:15]}...)")
    
    def _rotate_key(self) -> bool:
        """Rotates to the next API key. Returns False if all keys exhausted."""
        self.current_key_index += 1
        if self.current_key_index >= len(self.api_keys):
            self.current_key_index = 0  # Reset to first key
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
        model = MODELS.get(task, "openrouter/free")
        attempts = 0
        max_attempts = len(self.api_keys) * 2  # Try each key up to twice
        
        while attempts < max_attempts:
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
                    logger.warning(f"AI returned empty content on attempt {attempts + 1}. Retrying...")
                    attempts += 1
                    self._rotate_key()
                    continue
                
                return content.strip()
                
            except Exception as e:
                error_msg = str(e)
                logger.error(f"AI generation failed (attempt {attempts + 1}): {error_msg}")
                
                # Check if it's a rate limit or auth error
                if "429" in error_msg or "rate" in error_msg.lower():
                    logger.warning("Rate limited. Rotating API key...")
                elif "401" in error_msg or "403" in error_msg:
                    logger.warning("Authentication failed. Rotating API key...")
                elif "404" in error_msg:
                    logger.warning(f"Model '{model}' not found. Trying fallback...")
                    model = "meta-llama/llama-3.1-8b-instruct:free"  # Fallback to a highly reliable model
                
                has_more_keys = self._rotate_key()
                attempts += 1
                
                if not has_more_keys and attempts >= len(self.api_keys):
                    # All keys exhausted, log critical alert
                    logger.critical("ALL API KEYS EXHAUSTED. Cannot generate content.")
                    if self.db:
                        await self.db.log_alert(
                            level="CRITICAL",
                            module="AIEngine",
                            message=f"All {len(self.api_keys)} API keys failed. Last error: {error_msg}"
                        )
                    return None
        
        return None
    
    async def synthesize_news(self, raw_articles: List[Dict], niche_context: str) -> Optional[Dict]:
        """
        Takes 2-3 raw news articles about the same story and synthesizes them
        into a Daily Pulse PK post with Pakistani/South Asian lens.
        
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
        
        system_prompt = """You are a senior news editor for "Daily Pulse" — a Telegram channel 
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
            max_tokens=800,
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
    
    async def generate_image_headline(self, telegram_text: str) -> Optional[str]:
        """Generates a short, punchy 5-8 word headline for the PIL image overlay."""
        system_prompt = (
            "You are a headline writer. You output ONLY a short punchy news headline of 5-8 words. "
            "No quotes, no explanations, no preamble, no questions. Just the headline text itself. "
            "Example input: 'The Federal Reserve held interest rates today...'"
            "Example output: Fed Holds Rates Amid Inflation Fears"
        )
        user_prompt = f"Write a 5-8 word headline for this news:\n\n{telegram_text[:300]}"
        
        result = await self.generate(
            task="headline",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=30,
            temperature=0.5
        )
        
        if result:
            # Strip any quotes or extra formatting the AI might add
            result = result.strip('"\'\'\n').strip()
            # If it's too long (AI went rogue), truncate
            if len(result.split()) > 12:
                result = ' '.join(result.split()[:8])
        
        return result
