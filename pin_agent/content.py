"""
Pin copywriting.

Produces the title and description for a pin. Runs through Novi's AIEngine so
it inherits the narration guards built there — a model that answers with "We
need to write a Pinterest title..." would otherwise have that published, which
is exactly what happened on the news side.

Angles are rotated deliberately. The same product described the same way every
time reads as automation to both readers and Pinterest; the angle changes what
the copy leads with.
"""
import json
import logging
import random
import re
from typing import Dict, List, Optional

logger = logging.getLogger("PinAgent.Content")

# How a product is framed. Each is a genuinely different opening, not a
# reworded version of the same sentence.
ANGLES = {
    "problem_solver": "Lead with the everyday annoyance this removes.",
    "gift_idea": "Frame it as a gift, and say who it suits.",
    "small_kitchen": "Lead with saving space in a small kitchen.",
    "time_saver": "Lead with the minutes it saves during cooking or cleanup.",
    "hosting": "Frame it around cooking for guests or entertaining.",
    "organisation": "Lead with tidiness and keeping a worktop clear.",
}

# Pinterest truncates titles around 100 characters and rewards descriptions
# that read like a person wrote them, so these are targets rather than caps.
TITLE_TARGET = 60
DESCRIPTION_TARGET = 300

DISCLOSURE = "#ad"


class PinCopywriter:
    """Turns a product into publishable pin copy."""

    def __init__(self, ai_engine, brand: str = "Novi", niche: str = "home_kitchen"):
        self.ai = ai_engine
        self.brand = brand
        self.niche = niche
        self.written = 0

    # ── prompt ───────────────────────────────────────────────────

    def _system_prompt(self, angle_key: str) -> str:
        angle = ANGLES.get(angle_key, ANGLES["problem_solver"])
        return (
            "You write Pinterest pin copy for a home and kitchen shopping "
            "account. You output a single JSON object and nothing else.\n"
            "Begin your reply with { and end it with }. No explanation, no "
            "reasoning, no code fences.\n\n"
            "Keys:\n"
            '- "title": 6-11 words. Concrete and specific to THIS product. '
            "Written for someone browsing, not a product listing. No ALL CAPS, "
            "no exclamation marks, no emoji.\n"
            '- "description": 2-3 sentences, 40-55 words. Say what it does and '
            "who it helps. Plain, warm, human English.\n"
            '- "hashtags": 3-5 lowercase topic tags without the # symbol, '
            "relevant to kitchen and home searches.\n\n"
            f"Angle for this pin: {angle}\n\n"
            "Hard rules:\n"
            "- Never invent materials, dimensions, capacities or features that "
            "are not in the product data you are given.\n"
            "- Never state a price, a discount or a percentage. Prices change "
            "and the pin outlives them.\n"
            "- Never promise delivery times, guarantees or health benefits.\n"
            "- Do not use the words 'cheap', 'best ever', 'must have' or "
            "'life changing'."
        )

    @staticmethod
    def _user_prompt(product: Dict) -> str:
        # Only facts the listing actually gives. Rating and order count are
        # included as context for tone, and explicitly marked as not for
        # quoting, because "rated 4.8 by 4210 buyers" ages badly.
        return (
            f"PRODUCT: {product.get('clean_title') or product.get('title', '')}\n"
            f"CATEGORY: {product.get('category_name', 'Home & Kitchen')}\n"
            f"CONTEXT (for tone only, never quote these numbers): "
            f"rating {product.get('rating', 0)}, "
            f"{product.get('orders', 0)} orders\n\n"
            "Write the pin copy as JSON."
        )

    # ── generation ───────────────────────────────────────────────

    def pick_angle(self, recent_angles: Optional[List[str]] = None) -> str:
        """
        Chooses an angle, avoiding whatever was used most recently.

        Without this the model settles into one voice and every pin in the
        feed opens the same way.
        """
        recent = set(recent_angles or [])
        available = [a for a in ANGLES if a not in recent] or list(ANGLES)
        return random.choice(available)

    async def write(self, product: Dict,
                    recent_angles: Optional[List[str]] = None) -> Optional[Dict]:
        """
        Returns {title, description, hashtags, angle} or None.

        None means nothing publishable was produced, and the caller should skip
        the product rather than fall back to the raw listing title — those are
        keyword soup and make terrible pins.
        """
        angle = self.pick_angle(recent_angles)

        raw = await self.ai.generate(
            task="social_caption",
            system_prompt=self._system_prompt(angle),
            user_prompt=self._user_prompt(product),
            max_tokens=500,
            temperature=0.8,          # copy should vary between pins
            validator=self._is_usable,
            min_attempts=4,
        )
        if not raw:
            logger.warning(f"No usable copy for '{product.get('title', '')[:50]}'.")
            return None

        parsed = self._parse(raw)
        if not parsed:
            logger.warning("Pin copy did not parse as JSON.")
            return None

        title = self._tidy_title(parsed.get("title", ""))
        description = self._build_description(parsed)

        if len(title) < 12 or len(description) < 40:
            logger.warning(f"Pin copy too thin: title={len(title)}, "
                           f"description={len(description)}.")
            return None

        self.written += 1
        return {"title": title, "description": description,
                "hashtags": parsed.get("hashtags", []), "angle": angle}

    # ── validation and cleanup ───────────────────────────────────

    @staticmethod
    def _is_usable(text: str) -> bool:
        """Cheap structural check, run before the JSON is parsed."""
        if not text or "{" not in text or "}" not in text:
            return False
        lowered = text.lower()
        # A price anywhere in the reply means the model ignored the rule; the
        # compliance gate would reject the pin later anyway.
        if re.search(r"[$£€]\s?\d", text):
            return False
        return '"title"' in lowered and '"description"' in lowered

    @classmethod
    def _parse(cls, raw: str) -> Optional[Dict]:
        """Pulls the JSON object out of a reply that may have prose around it."""
        text = (raw or "").strip()
        text = re.sub(r"^```[a-z]*\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)

        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None

        blob = text[start:end + 1]
        try:
            parsed = json.loads(blob)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return cls._salvage(blob)

    # The fields, read straight out of malformed JSON.
    _FIELD = r'"{0}"\s*:\s*"((?:[^"\\]|\\.)*)"'

    @classmethod
    def _salvage(cls, blob: str) -> Optional[Dict]:
        """
        Reads the fields out of JSON that will not parse.

        One product in four came back with a reply the parser rejected --
        an unescaped quote or a raw newline inside a string is enough --
        and the product was skipped entirely after four attempts. The text
        itself was fine; only the punctuation around it was wrong.

        Only ever used as a fallback, and it returns None unless BOTH
        required fields are found, so a genuinely broken reply is still
        refused rather than published half-written.
        """
        title = re.search(cls._FIELD.format("title"), blob, re.S)
        description = re.search(cls._FIELD.format("description"), blob, re.S)
        if not (title and description):
            return None

        def unescape(value: str) -> str:
            return (value.replace('\\"', '"').replace("\\n", " ")
                         .replace("\\/", "/").replace("\\\\", "\\")).strip()

        tags: List[str] = []
        array = re.search(r'"hashtags"\s*:\s*\[(.*?)\]', blob, re.S)
        if array:
            tags = re.findall(r'"((?:[^"\\]|\\.)*)"', array.group(1))
        else:
            single = re.search(cls._FIELD.format("hashtags"), blob, re.S)
            if single:
                tags = [unescape(single.group(1))]

        logger.info("Pin copy JSON was malformed; fields salvaged from it.")
        return {"title": unescape(title.group(1)),
                "description": unescape(description.group(1)),
                "hashtags": tags}

    @staticmethod
    def _tidy_title(title: str) -> str:
        """Strips the shouting and trailing punctuation Pinterest dislikes."""
        text = re.sub(r"\s+", " ", str(title or "")).strip().strip('"')
        text = text.replace("!", "").strip()
        if text.isupper():
            text = text.title()
        if len(text) > 95:
            cut = text[:95]
            space = cut.rfind(" ")
            text = (cut[:space] if space > 30 else cut).rstrip(",;:-")
        return text

    @classmethod
    def _build_description(cls, parsed: Dict) -> str:
        """
        Assembles the description and guarantees the disclosure.

        The disclosure is appended in code, never left to the model: Pinterest
        requires it on every affiliate pin, and a model that forgets it once
        costs the account.
        """
        body = re.sub(r"\s+", " ", str(parsed.get("description", ""))).strip()

        # Split on commas AND whitespace AND stray hashes.
        #
        # Splitting on commas alone turned "organizer homehacks declutter"
        # into ONE tag: the separator survived the split, then the
        # punctuation strip below removed the spaces and produced
        # #organizerhomehacksdeclutter. Useless for discovery, and it looks
        # broken. The model returns the list both ways depending on the run,
        # so both have to work.
        raw_tags = parsed.get("hashtags") or []
        if isinstance(raw_tags, str):
            # ONE string, so the spaces between words are separators.
            tags: List[str] = [t for t in re.split(r"[,\s#]+", raw_tags) if t]
        else:
            # A LIST, so each element is already one tag even when it
            # contains spaces -- "kitchen organization" is a single tag and
            # splitting it would publish #kitchen #organization, which are
            # two far vaguer searches than the one actually meant.
            tags = [str(t) for t in raw_tags if str(t).strip()]

        clean_tags = []
        for tag in tags[:5]:
            tag = re.sub(r"[^a-z0-9]", "", str(tag).lower())
            # A one-letter tag is noise, and Pinterest caps tag length.
            if 2 < len(tag) <= 30 and tag not in clean_tags:
                clean_tags.append(tag)

        parts = [body]
        if clean_tags:
            parts.append(" ".join(f"#{t}" for t in clean_tags))
        parts.append(DISCLOSURE)

        description = "\n\n".join(p for p in parts if p)
        if len(description) > 780:
            description = description[:780].rsplit(" ", 1)[0] + f"\n\n{DISCLOSURE}"
        return description
