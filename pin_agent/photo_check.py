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
instruments for "kitchen scissors".

IT DESCRIBES, THEN CODE DECIDES. The first version asked the model straight
out whether a photograph was suitable, and tuning that question was a
losing game: one wording approved a supermarket freezer aisle as a
photograph of "a fridge", the next refused thirty-four of thirty-five
candidates and more than half of the pictures already chosen by hand. A
judgement drifts between runs and between models, and there is no way to
test it except to keep spending quota on it.

So the model is asked only what it can answer the same way every time --
what is in the picture -- and the DECISION is plain code against that
answer. "A supermarket freezer aisle with glass doors" is rejected because
it says supermarket, not because a model felt unsure. The rule is readable,
the failure is debuggable, and a fixed set of images gives the same result
today and next month. It is the same division as everywhere else here: the
model does the fuzzy part, code makes the call that matters.

THE BYTES ARE SENT, NOT THE URL. StockSnap answers a server-side fetch with
403, so passing the address gets an error rather than a verdict. The photo
is downloaded anyway to build the pin, so this costs nothing extra.

QUOTA. The free allowance is twenty requests a day PER PROJECT PER MODEL, so
the verifier walks a grid of every key against every model -- two projects
and three models is a hundred and twenty checks a day, against a need of
four to eight.
"""
import base64
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("PinAgent.PhotoCheck")

ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
            "{model}:generateContent")

# Each carries its own daily allowance, so one running out simply hands the
# next the job rather than stopping the day's work.
MODELS = (
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
)

TIMEOUT = 90.0

# The only thing the model is asked. Short, literal, and the same question
# every time -- which is what makes the result stable enough to test.
DESCRIBE = ("Describe this photograph in under 14 words. Say plainly what "
            "is in it and where it was taken. If it is not a photograph -- "
            "a drawing, painting, illustration, diagram or museum object -- "
            "say so first. Then add one of BRIGHT, PLAIN or DARK: BRIGHT if "
            "it is well lit, tidy and attractive; DARK if it is dim, "
            "cluttered or drab; PLAIN otherwise.")

# Pinterest's home-organisation audience saves bright, airy, uncluttered
# rooms. The first two advice pins that published were both CORRECT -- the
# right subject, a real home, everything the rules asked for -- and both
# were dowdy: a purple plastic spray trigger, and a cluttered kitchen with
# someone's back in the frame. Correct is not the same as saveable.
#
# So the describer grades the picture too, and a bright one is taken over a
# plain one when both are available. DARK is refused outright.
LOOK_BRIGHT = "bright"
LOOK_PLAIN = "plain"
LOOK_DARK = "dark"

# A description containing any of these means the picture cannot be used.
#
# Every entry earned its place from something that actually came back. The
# commercial words caught a supermarket freezer aisle that had been approved
# for a pin about a fridge door; the artwork words caught a Yale painting
# called "Kitchen Shelf" and a Victorian engraving of surgical instruments;
# the outdoor words caught Nebraska fossil beds returned for "a bed".
REJECT = {
    # somewhere that is not a home
    "commercial": ("supermarket", "grocery store", "shop", "store shelf",
                   "storefront", "retail", "warehouse", "restaurant",
                   "commercial kitchen", "hotel", "office", "showroom",
                   "aisle", "checkout", "cafe", "café", "factory",
                   "hospital", "classroom", "laboratory", "market stall",
                   "supermarket aisle", "display case", "vending"),
    # not a photograph at all
    "not a photo": ("illustration", "drawing", "painting", "sketch",
                    "clipart", "clip art", "cartoon", "diagram", "artwork",
                    "engraving", "lithograph", "etching", "watercolour",
                    "watercolor", "render", "3d model", "graphic", "icon",
                    "logo", "poster", "screenshot", "vector"),
    # a thing in a collection rather than a thing in use
    "museum": ("museum", "gallery", "exhibition", "artefact", "artifact",
               "antiquity", "ancient", "archaeological", "sculpture",
               "specimen", "fossil"),
    # the wrong world entirely
    "outdoors": ("landscape", "mountain", "beach", "forest", "meadow",
                 "field of", "wildlife", "dinosaur", "canyon", "desert",
                 "waterfall", "riverbank"),
}

# Words that mean the same thing to a person and different things to a
# string comparison. Without these "fridge" never matches a description
# saying "refrigerator", and a perfectly good photograph is thrown away.
SYNONYMS = {
    "fridge": {"refrigerator", "freezer", "icebox"},
    "refrigerator": {"fridge", "freezer"},
    "cupboard": {"cabinet", "press"},
    "cabinet": {"cupboard"},
    "worktop": {"counter", "countertop", "benchtop"},
    "countertop": {"counter", "worktop"},
    "counter": {"worktop", "countertop"},
    "bin": {"basket", "container", "tub", "caddy"},
    "jar": {"jars", "container", "canister"},
    "loo": {"toilet"},
    "hob": {"stove", "cooker", "stovetop"},
    "stove": {"hob", "cooker", "stovetop", "oven"},
    "flat": {"apartment"},
    "apartment": {"flat"},
    "wardrobe": {"closet", "armoire"},
    "closet": {"wardrobe"},
    "tap": {"faucet"},
    "faucet": {"tap"},
    "rubbish": {"trash", "garbage", "waste"},
    "pantry": {"larder", "cupboard"},
}

# Too common to prove anything about relevance.
_WEAK = {
    "the", "a", "an", "and", "or", "with", "for", "your", "you", "of", "in",
    "on", "to", "is", "are", "it", "its", "at", "by", "from", "into", "that",
    "this", "some", "small", "large", "little", "big", "new", "old", "home",
    "house", "room", "space", "thing", "things", "item", "items", "photo",
    "photograph", "picture", "image", "shows", "showing", "taken", "white",
    "black", "grey", "gray", "wooden", "wood", "metal", "plastic", "glass",
    "modern", "clean", "tidy", "neat", "inside", "indoor", "indoors",
    # The grade the describer appends. Without these, a subject containing
    # "dark" would match a photograph graded DARK and count as relevant.
    "bright", "plain", "dark", "dim", "cluttered", "drab", "attractive",
    "lit", "well",
}

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0"}


def _words(text: str) -> set:
    """Meaningful words, singular and plural collapsed."""
    out = set()
    for w in re.findall(r"[a-z]+", (text or "").lower()):
        if len(w) < 3 or w in _WEAK:
            continue
        out.add(w)
        if w.endswith("es") and len(w) > 4:
            out.add(w[:-2])
        if w.endswith("s") and len(w) > 3:
            out.add(w[:-1])
    return out


def look_of(description: str) -> str:
    """
    BRIGHT, PLAIN or DARK, as the describer graded it.

    MATCHED IN CAPITALS ONLY, and that is not fussiness. The describer is
    asked to append the grade in capitals, while the same words appear in
    ordinary prose describing what is in the frame -- "a white towel hangs
    on a dark tiled bathroom wall" is a perfectly bright photograph of dark
    tiles, and a lowercase search threw it away.

    Defaults to PLAIN when no grade is found -- an ungraded photograph is
    usable, just not preferred, and a missing word must never become a
    rejection.
    """
    text = description or ""
    if re.search(r"\bDARK\b", text):
        return LOOK_DARK
    if re.search(r"\bBRIGHT\b", text):
        return LOOK_BRIGHT
    return LOOK_PLAIN


def banned_reason(description: str) -> Optional[str]:
    """Why this description disqualifies the photograph, or None."""
    text = (description or "").lower()
    for label, phrases in REJECT.items():
        for phrase in phrases:
            if phrase in text:
                return f"{label}: {phrase!r}"
    return None


def is_relevant(description: str, subject: str) -> bool:
    """
    Whether the description and the search term are about the same thing.

    One shared meaningful word is enough, because the search already
    narrowed the pool -- this is catching the photograph that has nothing
    to do with the query, not ranking the ones that do.
    """
    wanted = _words(subject)
    if not wanted:
        return True
    got = _words(description)
    if wanted & got:
        return True
    for w in wanted:
        if SYNONYMS.get(w, set()) & got:
            return True
    return False


def judge(description: str, subject: str) -> Tuple[bool, str]:
    """The whole decision, in plain code. Returns (usable, why)."""
    if not (description or "").strip():
        return False, "no description"
    reason = banned_reason(description)
    if reason:
        return False, reason
    if not is_relevant(description, subject):
        return False, f"nothing in common with {subject!r}"
    if look_of(description) == LOOK_DARK:
        return False, "dim, cluttered or drab"
    return True, "ok"


class PhotoVerifier:
    """Describes a photograph, then decides in code whether it may be used."""

    def __init__(self, api_keys=None, models: Optional[Tuple[str, ...]] = None):
        if isinstance(api_keys, str):
            keys = [api_keys]
        elif api_keys:
            keys = list(api_keys)
        else:
            # EVERY SPELLING, because the quota is per project and a key
            # that is set under a name nothing reads is a key that does
            # not exist. Render was carrying GEMINI_API_KEY_1 and _3 while
            # this looked for GEMINI_API_KEY and _2 -- so one of the two
            # projects was silently doing nothing and the daily allowance
            # was half what it looked like.
            keys = [os.getenv(name, "") for name in
                    ("GEMINI_API_KEY", "GEMINI_API_KEY_1",
                     "GEMINI_API_KEY_2", "GEMINI_API_KEY_3",
                     "GEMINI_API_KEY_4")]
        self.api_keys = [k.strip() for k in keys if k and k.strip()]
        self.models = tuple(models or MODELS)
        self.checked = 0
        self.approved = 0
        self.rejected = 0
        self.reasons: Dict[str, int] = {}
        # (key index, model) pairs that have answered "out of quota" today.
        self._spent: set = set()
        self.last_error = ""
        self.last_description = ""

        if not self.api_keys:
            logger.warning(
                "No GEMINI_API_KEY set. Photographs cannot be verified, so "
                "only the hand-checked bank may publish.")

    @property
    def _pairs(self) -> List[Tuple[int, str]]:
        """Every key against every model. Quota is per project per model."""
        return [(i, m) for i in range(len(self.api_keys)) for m in self.models]

    @property
    def is_ready(self) -> bool:
        return bool(self.api_keys) and len(self._spent) < len(self._pairs)

    @property
    def status(self) -> Dict:
        return {
            "keys": len(self.api_keys),
            "models": list(self.models),
            "daily_capacity": len(self._pairs) * 20,
            "checked": self.checked,
            "approved": self.approved,
            "rejected": self.rejected,
            "reasons": dict(self.reasons),
            "spent_today": len(self._spent),
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

    # ── the description ──────────────────────────────────────────

    async def describe(self, image: bytes,
                       mime: str = "image/jpeg") -> Optional[str]:
        """
        What the model says is in the picture, or None if none could answer.

        None is not an empty description and the caller depends on it: an
        empty description means the photograph is unusable, None means
        nobody was left to ask.
        """
        import httpx

        if not self.api_keys or not image:
            return None

        body = {
            "contents": [{"parts": [
                {"text": DESCRIBE},
                {"inline_data": {"mime_type": mime,
                                 "data": base64.b64encode(image).decode()}},
            ]}],
            "generationConfig": {"maxOutputTokens": 600, "temperature": 0},
        }

        for key_index, model in self._pairs:
            if (key_index, model) in self._spent:
                continue
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                    r = await client.post(
                        ENDPOINT.format(model=model),
                        params={"key": self.api_keys[key_index]}, json=body)
            except Exception as e:
                self.last_error = type(e).__name__
                continue

            if r.status_code == 429:
                self._spent.add((key_index, model))
                continue
            if r.status_code != 200:
                self.last_error = f"HTTP {r.status_code}"
                logger.warning(f"{model} returned {r.status_code}: "
                               f"{r.text[:120]}")
                continue

            try:
                parts = ((r.json().get("candidates") or [{}])[0]
                         .get("content") or {}).get("parts") or []
                text = " ".join(p.get("text", "") for p in parts).strip()
            except Exception:
                continue
            if text:
                return text

        self.last_error = self.last_error or "every key and model is spent"
        return None

    # ── the verdict ──────────────────────────────────────────────

    async def verify(self, image: bytes, subject: str,
                     mime: str = "image/jpeg",
                     caption: str = "") -> Optional[bool]:
        """
        Whether this photograph may illustrate this subject.

        Returns None when no model could answer -- every quota spent, or the
        service unreachable. The caller must NOT treat that as approval.
        """
        if not image or not subject.strip():
            return None

        description = await self.describe(image, mime)
        if description is None:
            logger.warning(f"No description for '{subject[:40]}': "
                           f"{self.last_error}")
            return None

        self.last_description = description
        usable, why = judge(description, subject)
        self.checked += 1
        self.approved += usable
        self.rejected += not usable
        if not usable:
            self.reasons[why.split(":")[0]] = \
                self.reasons.get(why.split(":")[0], 0) + 1

        logger.info(f"'{subject[:28]}' -> {'ok' if usable else 'REJECTED ' + why}"
                    f"  [{description[:60]}]")
        return usable

    async def verify_url(self, url: str, subject: str,
                         caption: str = "") -> Optional[bool]:
        """Downloads and verifies in one step."""
        data = await self.fetch(url)
        if data is None:
            # Cleared so a caller reading last_description after a failed
            # download does not see the previous photograph's answer.
            self.last_description = ""
            self.last_error = "could not download the photograph"
            return None
        mime = "image/png" if url.lower().endswith(".png") else "image/jpeg"
        return await self.verify(data, subject, mime, caption)

    async def first_approved(self, urls: List[str], subject: str,
                             limit: int = 4,
                             caption: str = "") -> Optional[str]:
        """
        The first photograph in the list the rules accept.

        Candidates arrive best-first from the search, so this usually costs
        one call. `limit` stops a bad query spending the day's allowance on
        a single tip.
        """
        first_plain = None
        for url in list(urls)[:limit]:
            if not await self.verify_url(url, subject, caption):
                continue
            if look_of(self.last_description) == LOOK_BRIGHT:
                return url
            # Usable, but keep looking for a brighter one. The first two
            # advice pins to publish were both correct and both dowdy, and
            # a dowdy pin is not one anybody saves.
            if first_plain is None:
                first_plain = url
        if first_plain:
            logger.info(f"No bright photograph for '{subject[:32]}'; "
                        f"using a plain one.")
        return first_plain
