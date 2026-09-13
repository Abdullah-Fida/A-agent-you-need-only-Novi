"""
Does this photograph actually show what the pin says it shows?

WHY A MODEL LOOKS AT THE PICTURE.
Every other check in this pipeline reads TEXT ABOUT the picture -- its
title, its tags, its dimensions, its licence -- and text about a picture is
not the picture. Searching "under the bed" returned a photograph of Ashfall
Fossil Beds in Nebraska: correct resolution, public domain, a real
photograph, and the word "beds" right there in the title. It passed every
automated check there was. So did an Egyptian canopic jar for "jar lid", a
brass telescope for "hooks on a wall", and a Victorian engraving of surgical
instruments for "kitchen scissors". Roughly half of sixty queries came back
with something that would have looked broken on the board.

The only check that catches those is one that looks. Tested against the
forty-five photographs that had already been chosen by hand, it agreed with
eighteen of twenty and the two it rejected it was right about -- one was
captioned "bathroom shelf" and had no shelf in it, the other was captioned
for a tip about onions and potatoes and showed neither.

THE BYTES ARE SENT, NOT THE URL. StockSnap answers a server-side fetch with
403, so passing the address gets an error rather than a verdict. The photo
is downloaded anyway to build the pin, so this costs nothing extra.

VERIFY ONCE, STORE THE ANSWER. The free tier allows twenty requests per day
per model, which is nothing if every publish re-checks and plenty if a
photograph is checked once when it enters the bank and never again. Several
models each carry their own twenty, so the rotation below is what turns a
tight limit into a comfortable one.
"""
import asyncio
import base64
import logging
import os
from typing import List, Optional, Tuple

logger = logging.getLogger("PinAgent.PhotoCheck")

ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
            "{model}:generateContent")

# Tried in order. Each carries its OWN daily allowance -- the quota is
# "PerDayPerProjectPerModel" -- so a model that has run out simply hands the
# next one the job instead of stopping the day's work.
MODELS = (
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
)

TIMEOUT = 90.0

# Deliberately answerable with one word. A model asked to explain itself
# writes a paragraph, spends the token budget on reasoning, and returns
# nothing usable -- which is what happened on the first attempt.
PROMPT = (
    'A Pinterest pin will caption this photograph with: "{subject}".\n\n'
    'Answer YES only if ALL of these are true:\n'
    '  - the photograph clearly shows that subject\n'
    '  - it is a real photograph, not clipart, an illustration, a drawing, '
    'an artwork, a diagram or a museum object\n'
    '  - it is in focus, well lit, and would look good on Pinterest\n\n'
    'Otherwise answer NO.\n'
    'Reply with one word only: YES or NO.'
)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0"}


class PhotoVerifier:
    """Asks a vision model whether a photograph matches its caption."""

    def __init__(self, api_key: str = "", models: Optional[Tuple[str, ...]] = None):
        self.api_key = (api_key or os.getenv("GEMINI_API_KEY") or "").strip()
        self.models = tuple(models or MODELS)
        self.checked = 0
        self.approved = 0
        self.rejected = 0
        # Models that have answered "out of quota" today. Cleared by a
        # restart, which is the right granularity for a daily limit.
        self._exhausted: set = set()
        self.last_error = ""

        if not self.api_key:
            logger.warning(
                "GEMINI_API_KEY is not set. Photographs cannot be verified, "
                "so nothing new may enter the tip bank.")

    @property
    def is_ready(self) -> bool:
        return bool(self.api_key) and len(self._exhausted) < len(self.models)

    @property
    def status(self) -> dict:
        return {
            "configured": bool(self.api_key),
            "checked": self.checked,
            "approved": self.approved,
            "rejected": self.rejected,
            "models": list(self.models),
            "exhausted_today": sorted(self._exhausted),
            "last_error": self.last_error,
        }

    # ── the picture ──────────────────────────────────────────────

    @staticmethod
    async def fetch(url: str) -> Optional[bytes]:
        """Downloads a photograph. Returns None rather than raising."""
        if not (url or "").startswith("http"):
            return None
        try:
            import httpx
            async with httpx.AsyncClient(timeout=TIMEOUT,
                                         follow_redirects=True) as client:
                r = await client.get(url, headers=UA)
            if r.status_code != 200:
                logger.info(f"Photo fetch returned HTTP {r.status_code}.")
                return None
            return r.content
        except Exception as e:
            logger.info(f"Photo fetch failed: {type(e).__name__}")
            return None

    # ── the verdict ──────────────────────────────────────────────

    async def _ask(self, model: str, image: bytes, subject: str,
                   mime: str) -> Optional[bool]:
        """
        True/False for a verdict, None when this model could not answer.

        None and False mean different things and the caller depends on it:
        False is "that photograph is wrong", None is "ask someone else".
        Collapsing them would silently reject every photograph the moment a
        quota ran out.
        """
        import httpx

        body = {
            "contents": [{"parts": [
                {"text": PROMPT.format(subject=subject)},
                {"inline_data": {"mime_type": mime,
                                 "data": base64.b64encode(image).decode()}},
            ]}],
            "generationConfig": {"maxOutputTokens": 400, "temperature": 0},
        }
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await client.post(ENDPOINT.format(model=model),
                                      params={"key": self.api_key}, json=body)
        except Exception as e:
            self.last_error = f"{type(e).__name__}"
            return None

        if r.status_code == 429:
            self._exhausted.add(model)
            logger.info(f"{model} is out of quota for today; trying the next.")
            return None
        if r.status_code != 200:
            self.last_error = f"HTTP {r.status_code}"
            logger.warning(f"{model} returned {r.status_code}: {r.text[:120]}")
            return None

        try:
            parts = ((r.json().get("candidates") or [{}])[0]
                     .get("content") or {}).get("parts") or []
            text = " ".join(p.get("text", "") for p in parts).strip().upper()
        except Exception:
            return None

        if text.startswith("Y"):
            return True
        if text.startswith("N"):
            return False
        # An answer that is neither is not a rejection -- it is a model that
        # did not follow the instruction.
        self.last_error = f"unparsable answer: {text[:30]!r}"
        return None

    async def verify(self, image: bytes, subject: str,
                     mime: str = "image/jpeg") -> Optional[bool]:
        """
        Whether this photograph may illustrate this subject.

        Returns None when no model could answer -- every quota spent, or the
        service unreachable. The caller must NOT treat that as approval.
        """
        if not self.api_key or not image or not subject.strip():
            return None

        for model in self.models:
            if model in self._exhausted:
                continue
            verdict = await self._ask(model, image, subject, mime)
            if verdict is None:
                continue
            self.checked += 1
            self.approved += verdict
            self.rejected += not verdict
            logger.info(f"Photo for '{subject[:40]}': "
                        f"{'approved' if verdict else 'REJECTED'} ({model})")
            return verdict

        self.last_error = self.last_error or "every model is out of quota"
        logger.warning(f"No verdict for '{subject[:40]}': {self.last_error}")
        return None

    async def verify_url(self, url: str, subject: str) -> Optional[bool]:
        """Downloads and verifies in one step."""
        data = await self.fetch(url)
        if data is None:
            return None
        mime = "image/png" if url.lower().endswith(".png") else "image/jpeg"
        return await self.verify(data, subject, mime)

    async def first_approved(self, urls: List[str], subject: str,
                             limit: int = 4) -> Optional[str]:
        """
        The first photograph in the list that the model accepts.

        Candidates are tried in order and the search already returns them
        best-first, so this usually costs one call. `limit` stops a bad
        query burning the day's whole allowance on one tip.
        """
        for url in list(urls)[:limit]:
            if await self.verify_url(url, subject):
                return url
        return None
