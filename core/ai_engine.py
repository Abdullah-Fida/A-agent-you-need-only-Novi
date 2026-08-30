"""
Multi-Key OpenRouter AI Engine with automatic failover, retry, and self-healing.
Routes different tasks to different free models via a pool of API keys.
"""
import asyncio
import contextlib
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

# Groq's gpt-oss models reason before answering, and max_tokens covers the
# reasoning AND the answer. At the budgets callers actually pass, reasoning ate
# the whole allowance and the answer came back empty or truncated:
#
#   max_tokens=500,  default effort -> reasoning 2007 chars, content 0, "length"
#   max_tokens=500,  effort "low"   -> reasoning  234 chars, content 416, "stop"
#
# Low effort also cuts total completion tokens roughly fourfold, which matters
# against Groq's 8000-tokens-per-minute ceiling, and leaves less reasoning
# around to leak into published copy.
REASONING_EFFORT_MODELS = ("gpt-oss",)

# Enough room for reasoning plus an answer, used only when effort cannot be
# set and the caller's budget would otherwise be swallowed whole.
REASONING_MIN_TOKENS = 1600

# Models that exist in a provider catalogue but cannot hold a conversation.
NON_CHAT_MARKERS = ("whisper", "tts", "embed", "guard", "orpheus", "moderation")

# Last-resort chain for OpenRouter when its catalogue cannot be read. This is
# NOT provider-neutral -- see _fallback_candidates, which prefers whatever the
# configured provider actually serves. Sending these ids to Groq 404s.
FALLBACK_MODELS = [
    "openrouter/free",
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "inclusionai/ling-3.0-tiny:free",
]

# Which tasks need a large model and which do not.
#
# Groq allows 8000 tokens per MINUTE per key, so spending a 120b call on a
# six-line trading signal eats headroom that a news post or article needs.
# Signals are also time-sensitive, and the small model answers faster.
TASK_TIER = {
    "synthesizer":      "quality",   # the channel post — the main product
    "article":          "quality",   # long-form, needs the better writer
    "signal_cleansing": "fast",      # short, structured, must be quick
    "social_caption":   "fast",      # a few lines
    "seo":              "fast",      # short metadata
    "stealth":          "fast",      # one casual sentence
    "headline":         "fast",
}

# The actual model per provider for each tier.
TIER_MODELS = {
    "groq": {
        "quality": "openai/gpt-oss-120b",
        "fast":    "openai/gpt-oss-20b",
    },
    "openrouter": {
        "quality": "openrouter/free",
        "fast":    "openrouter/free",
    },
    "openai": {
        "quality": "gpt-4o",
        "fast":    "gpt-4o-mini",
    },
}

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
                 label: str = "AI", backup_keys: Optional[List[str]] = None,
                 backup_provider: str = "", backup_model: str = "",
                 backup_base_url: str = ""):
        """
        Args:
            api_keys:      one or more keys, rotated on failure
            base_url:      explicit endpoint; overrides `provider`
            provider:      'openrouter' | 'groq' | 'openai'
            default_model: model used when a task has no specific mapping
            label:         name used in logs, e.g. 'ArticleAI'
            backup_keys:   keys for a SECOND provider, used only when every
                           model on the first one has failed. Model-level
                           fallback cannot survive the provider itself being
                           down, and that outage stops the whole bot.
        """
        if not api_keys:
            raise ValueError(f"{label}: at least one API key is required.")

        self.api_keys = api_keys
        self.current_key_index = 0
        self._model_cache = None          # provider catalogue, read once
        # Flipped off permanently if the provider rejects the parameter, so one
        # unsupported deployment does not fail every later call.
        self._reasoning_effort_ok = True

        # Second provider, tried only when the first is completely exhausted.
        self.backup_keys = [k.strip() for k in (backup_keys or []) if k.strip()]
        self.backup_provider = (backup_provider or "").strip().lower()
        self.backup_model = (backup_model or "").strip()
        self.backup_base_url = (backup_base_url or "").strip()
        self._failing_over = False
        if self.backup_keys and self.backup_provider:
            logger.info(f"{label}: backup provider configured "
                        f"({self.backup_provider} / "
                        f"{self.backup_model or 'provider default'}).")

        self._retired_reported = set()    # one warning per retired model
        self.notification_manager = None  # wired by main after construction
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
    
    # Preferred models per provider, best first. Used to pick a replacement
    # when the configured model is not in the provider's live catalogue.
    #
    # This list is a preference, not a promise: providers retire models with
    # no warning — Groq removed the entire Llama family, which silently broke
    # both NOVI and the article agent — so whatever is actually available at
    # runtime wins over anything hardcoded here.
    PREFERRED_MODELS = {
        "groq": [
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "groq/compound",
            "groq/compound-mini",
            "qwen/qwen3.6-27b",
        ],
        "openrouter": [
            "openrouter/free",
            "google/gemma-4-31b-it:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
        ],
        "openai": ["gpt-4o-mini", "gpt-4o"],
    }

    async def available_models(self, force: bool = False) -> set:
        """
        The models this provider will actually serve, from its own catalogue.

        Cached for the process: the list changes on the provider's schedule,
        not ours, and a restart re-reads it. An unreachable catalogue returns
        an empty set, which callers treat as "unknown" and carry on rather
        than blocking every request behind a metadata call.
        """
        if self._model_cache is not None and not force:
            return self._model_cache

        try:
            models = await self.client.models.list()
            self._model_cache = {m.id for m in models.data}
            logger.info(f"{self.label}: provider offers {len(self._model_cache)} models.")
        except Exception as e:
            logger.warning(f"{self.label}: could not read the model catalogue "
                           f"({type(e).__name__}); continuing without it.")
            self._model_cache = set()
        return self._model_cache

    async def resolve_model(self, wanted: str) -> str:
        """
        Swaps a retired model for one the provider still serves.

        Without this a retirement is a silent outage: every call 404s until
        someone notices and edits an environment variable by hand.
        """
        catalogue = await self.available_models()
        if not catalogue or wanted in catalogue:
            return wanted

        for candidate in self.PREFERRED_MODELS.get(self.provider, []):
            if candidate in catalogue:
                logger.error(f"{self.label}: model '{wanted}' is no longer offered; "
                             f"using '{candidate}' instead.")
                await self._warn_model_retired(wanted, candidate)
                return candidate

        # Nothing preferred is available — take any chat-capable model rather
        # than fail outright.
        for candidate in sorted(catalogue):
            if not any(x in candidate for x in NON_CHAT_MARKERS):
                logger.error(f"{self.label}: falling back to '{candidate}'.")
                await self._warn_model_retired(wanted, candidate)
                return candidate

        logger.error(f"{self.label}: no usable model found for '{wanted}'.")
        return wanted

    async def _warn_model_retired(self, wanted: str, replacement: str):
        """Reports a retirement once, so the config gets corrected properly."""
        if wanted in self._retired_reported:
            return
        self._retired_reported.add(wanted)
        if self.db:
            await self.db.log_error(
                module=f"AIEngine[{self.label}]",
                error_type="ModelRetired",
                error_message=(f"'{wanted}' is no longer offered by {self.provider}; "
                               f"now using '{replacement}'. Update the configuration."),
                auto_resolved=True,
            )
        if self.notification_manager:
            await self.notification_manager.send_notification(
                subject=f"Model retired: {wanted}",
                message=(f"{self.provider} no longer offers '{wanted}'.\n\n"
                         f"The bot switched to '{replacement}' by itself and is "
                         f"still working — nothing is broken and nothing needs "
                         f"doing right now.\n\n"
                         f"When convenient, set the model to '{replacement}' in "
                         f"Render so the startup choice matches what is in use."),
                is_critical=False,
            )

    # ── output sanitising ────────────────────────────────────────────
    #
    # Openings that mean the model started thinking out loud instead of
    # answering. Matched only at the very start of the reply, so an article
    # that legitimately contains "let me explain" mid-paragraph is untouched.
    _NARRATION_OPENERS = re.compile(
        r"^\s*(?:"
        r"here(?:'|\u2019)?s?\s+(?:a\s+)?(?:my\s+)?thinking|"
        r"here(?:'|\u2019)?s?\s+how\s+i\b|"
        r"we\s+(?:need|must|should|have)\s+to\b|"
        r"we\s+are\s+(?:asked|given)\b|"
        r"the\s+user\s+(?:wants|asked|is\s+asking)|"
        r"the\s+(?:instruction|prompt|task|request)s?\b|"
        r"let(?:'|\u2019)?s\s+|let\s+me\b|"
        r"i\s+(?:need|should|must|will|am\s+asked)\b|"
        r"first,?\s+i\b|"
        r"okay,?\s+so\b|alright,?\s+so\b|"
        r"to\s+(?:answer|solve|do)\s+this\b|"
        r"analy[sz]ing\s+the\b|"
        r"looking\s+at\s+the\s+(?:raw|given|provided)\b|"
        r"your\s+(?:job|task)\b|"
        r"as\s+an\s+ai\b|"
        r"step\s*1\s*[:.]|"
        r"\*\*(?:analyz|understand|step|thinking)"
        r")",
        re.I,
    )

    # Unambiguous narration, wherever it appears in the opening.
    _NARRATION_PHRASES = (
        "let me analyz", "let me check", "let me think", "let me look",
        "let me first", "let me start", "let's analyz", "let's check",
        "thinking process", "we need to", "the user wants", "the user asked",
        "looking at the raw", "to determine if it", "i need to determine",
        "step 1:", "my task is", "the task is to", "as an ai",
    )

    # Whole-reply tells: the model quoting its own instructions back.
    _ECHO_MARKERS = (
        "output only", "do not add any", "respond with only",
        "system prompt", "you are a professional", "your job:",
    )

    @classmethod
    def _strip_reasoning(cls, content: str) -> str:
        """
        Removes a model's visible chain-of-thought, returning only the answer.

        Reasoning models wrap thinking in <think> tags, or separate it from
        the answer with a "Final answer:" style label. Whatever follows the
        last such marker is the real output.
        """
        if not content:
            return ""
        text = content.strip()

        # Tagged thinking, closed or left open by a token cut-off
        text = re.sub(r"<(think|thinking|reasoning|scratchpad)>.*?</\1>", "",
                      text, flags=re.S | re.I)
        text = re.sub(r"^.*?</(?:think|thinking|reasoning|scratchpad)>", "",
                      text, flags=re.S | re.I).strip()
        text = re.sub(r"<(?:think|thinking|reasoning|scratchpad)>.*$", "",
                      text, flags=re.S | re.I).strip()

        # An explicit answer label — take everything after the last one
        label = list(re.finditer(
            r"^\s*(?:\*\*|##\s*)?(?:final\s+(?:answer|output|response)|"
            r"here\s+is\s+the\s+(?:final\s+)?(?:answer|output|post|signal|caption)|"
            r"output)\s*(?:\*\*)?\s*[:\-]\s*",
            text, re.I | re.M))
        if label:
            text = text[label[-1].end():].strip()

        # A whole reply wrapped in one code fence
        fenced = re.fullmatch(r"```[a-z]*\s*\n(.*?)\n?```", text, re.S | re.I)
        if fenced:
            text = fenced.group(1).strip()

        return text.strip()

    @classmethod
    def looks_like_narration(cls, text: str) -> bool:
        """True when the reply is the model talking about the task, not doing it."""
        if not text:
            return True
        stripped = text.strip()
        if cls._NARRATION_OPENERS.match(stripped):
            return True
        # A tell can sit just behind a few junk words ("Here in andells, let me
        # analyze the raw signal..."). _NARRATION_OPENERS is ^-anchored, so a
        # separate unanchored list scans the opening. Kept to phrases that
        # published copy would not contain, and limited to the first 200
        # characters, so a real article discussing a topic is unaffected.
        opening = stripped[:200].lower()
        if any(phrase in opening for phrase in cls._NARRATION_PHRASES):
            return True
        head = stripped[:400].lower()
        return any(marker in head for marker in cls._ECHO_MARKERS)

    def _fallback_candidates(self) -> List[str]:
        """
        Models worth trying next, for THIS provider.

        The hardcoded FALLBACK_MODELS list is OpenRouter's. Walking it while
        configured for Groq sent `openrouter/free` to Groq and 404'd every
        attempt, turning one bad answer into a total failure. So the
        provider's own catalogue wins, exactly as it does in resolve_model.

        Reads the catalogue cache rather than fetching: by the time anything
        is falling back, a request has already been made and populated it, and
        this has to stay callable from synchronous code.
        """
        catalogue = self._model_cache or set()
        preferred = self.PREFERRED_MODELS.get(self.provider, [])

        if not catalogue:
            # Catalogue unreadable. Preferences are still provider-correct;
            # the OpenRouter chain is only right when that is the provider.
            chain = list(preferred) + (
                FALLBACK_MODELS if self.provider == "openrouter" else [])
            return list(dict.fromkeys(chain))

        candidates = [m for m in preferred if m in catalogue]
        candidates += [m for m in sorted(catalogue)
                       if m not in candidates
                       and not any(x in m for x in NON_CHAT_MARKERS)]
        return candidates

    def _next_model(self, current: str, tried: set) -> str:
        """
        Picks a different model after a bad answer.

        Retrying the same model tends to reproduce the same narration, so a
        rejected answer moves on and only rotates keys once the list is spent.
        """
        for candidate in self._fallback_candidates():
            if candidate not in tried and candidate != current:
                tried.add(candidate)
                logger.info(f"Switching model to {candidate}.")
                return candidate
        self._rotate_key()
        return current

    def _model_for_task(self, task: str) -> str:
        """
        The model this task should run on.

        A configured model (NEWS_MODEL / ARTICLE_MODEL) sets the quality tier;
        short tasks still drop to the provider's smaller model. Pinning one
        model for everything is what a bare default_model used to do, and it
        spent a 120b call on every six-line trading signal.
        """
        tier = TASK_TIER.get(task, "quality")
        tier_models = TIER_MODELS.get(self.provider, {})

        if tier == "quality":
            return self.default_model or tier_models.get("quality") or DEFAULT_MODEL

        fast = tier_models.get("fast")
        if fast:
            return fast
        # Unknown provider: the configured model is the only thing we can trust.
        return self.default_model or DEFAULT_MODEL

    def _can_fail_over(self) -> bool:
        """Whether a second provider is available and not already in use."""
        return bool(self.backup_keys and self.backup_provider
                    and not self._failing_over)

    @contextlib.asynccontextmanager
    async def _on_backup_provider(self):
        """
        Swaps in the backup provider for one call, then restores everything.

        The client, keys, provider, model and the cached model catalogue all
        belong to a provider, so all of them move together -- leaving the
        catalogue behind would offer Groq model names to OpenRouter, which is
        the bug this whole fallback path exists to avoid.
        """
        saved = (self.client, self.api_keys, self.current_key_index,
                 self.provider, self.base_url, self.default_model,
                 self._model_cache, self._reasoning_effort_ok)
        self._failing_over = True
        try:
            self.api_keys = self.backup_keys
            self.current_key_index = 0
            self.provider = self.backup_provider
            self.base_url = (self.backup_base_url
                             or self.PROVIDERS.get(self.backup_provider, ""))
            self.default_model = self.backup_model
            self._model_cache = None
            self._reasoning_effort_ok = True
            # _build_client assigns self.client rather than returning it.
            self._build_client()
            yield
        finally:
            (self.client, self.api_keys, self.current_key_index,
             self.provider, self.base_url, self.default_model,
             self._model_cache, self._reasoning_effort_ok) = saved
            self._failing_over = False

    async def generate(self, task: str, system_prompt: str, user_prompt: str,
                       max_tokens: int = 500, temperature: float = 0.7,
                       validator=None, min_attempts: int = 3) -> Optional[str]:
        """
        Generates AI text with automatic key rotation on failure.
        
        Args:
            task: The task type key (e.g., 'synthesizer', 'headline', 'stealth')
            system_prompt: The system instruction for the AI
            user_prompt: The user-facing prompt
            max_tokens: Maximum response length
            temperature: Creativity level (0.0 = factual, 1.0 = creative)
            validator: Optional callable taking the cleaned text, returning
                True if it is publishable. A rejected answer is retried on
                a different model rather than returned.
        
        Returns:
            The generated text, or None if nothing publishable was produced.

        Never returns the model's thinking. The free router regularly serves
        reasoning models that narrate before answering ("We need to...",
        "Here's a thinking process:"), and that narration was reaching
        Telegram, the signal group and Facebook verbatim.
        """
        model = self._model_for_task(task)

        # Always give at least 3 tries even with a single key, and back off
        # between them so a transient 429 doesn't kill the whole post.
        # Confirm the provider still serves this model before spending the
        # attempt budget 404-ing against a retired one.
        model = await self.resolve_model(model)

        max_attempts = max(min_attempts, len(self.api_keys) * 2)
        tried_models = {model}
        last_error = ""

        for attempt in range(max_attempts):
            try:
                reasoning = any(m in model.lower() for m in REASONING_EFFORT_MODELS)
                kwargs = {}
                budget = max_tokens
                if reasoning and self._reasoning_effort_ok:
                    kwargs["extra_body"] = {"reasoning_effort": "low"}
                elif reasoning:
                    budget = max(max_tokens, REASONING_MIN_TOKENS)

                try:
                    response = await self.client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        max_tokens=budget,
                        temperature=temperature,
                        **kwargs
                    )
                except Exception as e:
                    if not kwargs or "reasoning_effort" not in str(e):
                        raise
                    # This deployment does not take the parameter. Buy the
                    # answer room instead, and stop asking.
                    logger.warning(f"{self.label}: provider rejected "
                                   f"reasoning_effort; using a larger budget.")
                    self._reasoning_effort_ok = False
                    response = await self.client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        max_tokens=max(max_tokens, REASONING_MIN_TOKENS),
                        temperature=temperature
                    )

                choice = response.choices[0]
                content = choice.message.content
                if not content or not content.strip():
                    if choice.finish_reason == "length":
                        logger.warning(
                            f"{self.label}: model '{model}' spent its whole "
                            f"{max_tokens}-token budget before answering. "
                            f"Retrying with more room.")
                        max_tokens = max(max_tokens * 2, REASONING_MIN_TOKENS)
                    logger.warning(f"AI returned empty content on attempt {attempt + 1}. Retrying...")
                    self._rotate_key()
                    await self._backoff(attempt)
                    continue

                content = self._strip_reasoning(content)
                if not content:
                    logger.warning(f"Only reasoning came back on attempt {attempt + 1}. Retrying...")
                    model = self._next_model(model, tried_models)
                    await self._backoff(attempt)
                    continue

                if self.looks_like_narration(content):
                    logger.warning(f"Model narrated instead of answering on attempt "
                                   f"{attempt + 1} ({model}). Retrying on another model.")
                    model = self._next_model(model, tried_models)
                    await self._backoff(attempt)
                    continue

                if validator is not None and not validator(content):
                    logger.warning(f"Output failed the '{task}' validator on "
                                   f"attempt {attempt + 1}. Retrying.")
                    model = self._next_model(model, tried_models)
                    await self._backoff(attempt)
                    continue

                return content

            except Exception as e:
                last_error = str(e)
                logger.error(f"AI generation failed (attempt {attempt + 1}/{max_attempts}): {last_error}")
                lowered = last_error.lower()

                # A retired or unavailable model never recovers by retrying,
                # so move to the next candidate immediately. Groq removing the
                # Llama family silently broke every call using it.
                if ("does not exist" in lowered or "not exist or you do not have" in lowered
                        or "model_not_found" in lowered or "decommissioned" in lowered):
                    logger.error(f"Model '{model}' is unavailable — switching.")
                    model = self._next_model(model, tried_models)
                    await self._backoff(attempt)
                    continue

                if "429" in last_error or "rate" in lowered:
                    logger.warning("Rate limited. Rotating key and backing off...")
                    self._rotate_key()
                elif "401" in last_error or "403" in last_error:
                    logger.warning("Authentication failed. Rotating API key...")
                    self._rotate_key()
                elif "404" in last_error or "not a valid model" in lowered or "unavailable" in lowered:
                    # A 404 means the model is gone, not busy. A dedicated
                    # engine used to be pinned to its model here and simply
                    # retried, which turned every provider retirement into a
                    # hard outage -- the failure resolve_model exists to stop.
                    chain = self._fallback_candidates()
                    nxt = next((m for m in chain
                                if m not in tried_models and m != model), None)
                    if nxt:
                        logger.warning(f"Model '{model}' unavailable. Falling back to '{nxt}'.")
                        model = nxt
                        tried_models.add(nxt)
                    else:
                        logger.error("All fallback models exhausted.")
                else:
                    self._rotate_key()

                await self._backoff(attempt)

        # Every model on this provider is exhausted. If a second provider is
        # configured, try it before giving up: model-level fallback cannot
        # help when the provider itself is down, rate-limited or unreachable,
        # and that outage takes the whole bot with it.
        if self._can_fail_over():
            logger.error(f"{self.label}: {self.provider} exhausted "
                         f"({last_error}). Failing over to {self.backup_provider}.")
            async with self._on_backup_provider():
                return await self.generate(
                    task=task, system_prompt=system_prompt, user_prompt=user_prompt,
                    max_tokens=max_tokens, temperature=temperature,
                    validator=validator, min_attempts=min_attempts)

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
    
