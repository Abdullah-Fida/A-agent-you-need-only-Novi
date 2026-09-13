"""
Writes a fresh tip for an advice pin, so the bank never runs out.

WHY THIS EXISTS.
The first version of the advice pins was forty-five tips written by hand,
each with a photograph chosen by eye. That was the right way to start -- it
proved the format and it gave the account something verified to publish --
but four advice pins a day empties forty-five in eleven days, and Pinterest
is explicit that a repeated pin gets no distribution at all: "re-uploading
the same pin repeatedly is one of the fastest ways to limit your reach or
get your account flagged."

So the bank is now a FALLBACK rather than the supply. Groq writes the tip,
Openverse finds a photograph for it, and the vision check confirms the
photograph actually shows what the tip is about. Nothing runs out and
nothing repeats.

The model writes TEXT ONLY. It never chooses the picture and it never
decides whether a pin may publish -- searching is a search and the compliance
gate is plain code, for the same reason it always was.

WHAT THE PROMPT IS FOR. A model asked for "a home organisation tip" returns
"Use baskets to organise your space!" every time: true, useless, and
indistinguishable from every other account in the niche. The prompt below
asks for a specific mechanism and a reason, bans the vocabulary that makes
copy read as generated, and hands over the recent titles so it writes
something new rather than rephrasing yesterday.
"""
import json
import logging
import random
import re
from typing import Dict, List, Optional

from pin_agent import boards as board_routing

logger = logging.getLogger("PinAgent.TipWriter")

# Pinterest reports titles in this range taking substantially more
# impressions than shorter ones -- long enough to carry the words people
# search, short enough not to be truncated.
TITLE_MIN = 40
TITLE_MAX = 60

BODY_MIN = 90
BODY_MAX = 320

# Phrases that make a pin read as machine-written. Most are the stock
# openers of AI copy; the rest are the empty intensifiers this niche is
# drowning in. A tip containing any of them is rejected and rewritten.
BANNED = (
    "game-changer", "game changer", "elevate", "transform your",
    "revolutionize", "revolutionise", "unlock", "unleash", "dive into",
    "in today's", "in this article", "look no further", "say goodbye",
    "level up", "must-have", "must have", "life-changing", "effortlessly",
    "seamlessly", "simply put", "the key is", "pro tip", "did you know",
    "ultimate", "amazing", "incredible", "perfect solution", "you'll love",
    "trust me", "believe it or not", "here's the thing", "let's face it",
    "moreover", "furthermore", "in conclusion", "delve",
)

# Anything that dates a pin. A pin is seen for months and a price or a sale
# stops being true long before it stops being shown.
STALE = re.compile(
    r"[$£€¥₹]\s?\d|\b\d{1,3}\s?(?:%|percent)\s?(?:off|cheaper|less)\b"
    r"|\b(?:on sale|sale price|cheapest|best price|this year|this month"
    r"|2025|2026|2027)\b", re.I)

SYSTEM = """You write short, genuinely useful home organisation tips for \
Pinterest.

You are writing for someone standing in their own kitchen or bathroom \
wondering why it never stays tidy. Give them ONE specific thing to do and \
the reason it works.

RULES
- The tip must be TRUE and useful on its own. It must NOT recommend buying \
anything, name a brand, or read as an advert. Nothing is being sold.
- Say the MECHANISM, not the outcome. "Rolled towels take a third less \
shelf depth than folded ones" is a tip. "Keep your towels tidy!" is not.
- Plain British English. Short sentences. Write the way a practical person \
speaks, not the way marketing copy reads.
- No exclamation marks. No emoji. No hashtags. No second-person commands \
stacked in a row.
- Never mention a price, a discount, a sale, or a year.
- Do not open with "Did you know", "Pro tip", "Say goodbye", "Transform", \
"Elevate", "Unlock", or any similar phrase.

OUTPUT
Return ONLY a JSON object, no other text:
{
  "title": "the tip itself, %d-%d characters, plain sentence case",
  "body": "%d-%d characters: why it works, concretely",
  "photo": "2-4 words naming the ORDINARY HOUSEHOLD OBJECT OR ROOM a \
photograph should show"
}

The "photo" field is a search term for a stock photo library. It must name \
something physical and common -- "bathroom sink", "spice jars", "folded \
laundry", "kitchen drawer". Never abstract ("organisation", "tidiness"), \
never a brand, never a person.""" % (TITLE_MIN, TITLE_MAX, BODY_MIN, BODY_MAX)


class TipWriter:
    """Turns a board into a fresh, publishable tip."""

    def __init__(self, ai_engine):
        self.ai = ai_engine
        self.written = 0
        self.rejected = 0
        self.last_error = ""

    # ── prompt ───────────────────────────────────────────────────

    @staticmethod
    def _user_prompt(board: str, avoid: List[str]) -> str:
        recent = "\n".join(f"- {t}" for t in (avoid or [])[:25])
        avoid_block = (
            f"\n\nThese tips have been published recently. Write about "
            f"something DIFFERENT -- a different object, a different part of "
            f"the room, a different problem. Do not rephrase any of them:\n"
            f"{recent}" if recent else "")
        return (f"Write one tip for a Pinterest board called "
                f"\"{board}\".{avoid_block}")

    # ── validation ───────────────────────────────────────────────

    @staticmethod
    def _looks_like_json(text: str) -> bool:
        return bool(text) and "{" in text and "}" in text and '"title"' in text

    @classmethod
    def _problems(cls, tip: Dict[str, str]) -> List[str]:
        """Every reason this tip is not publishable, so one pass fixes all."""
        out = []
        title = (tip.get("title") or "").strip()
        body = (tip.get("body") or "").strip()
        photo = (tip.get("photo") or "").strip()

        if not (TITLE_MIN <= len(title) <= TITLE_MAX):
            out.append(f"title is {len(title)} chars, needs "
                       f"{TITLE_MIN}-{TITLE_MAX}")
        if not (BODY_MIN <= len(body) <= BODY_MAX):
            out.append(f"body is {len(body)} chars, needs {BODY_MIN}-{BODY_MAX}")
        if not (1 <= len(photo.split()) <= 4):
            out.append("photo term must be 1-4 words")

        blob = f"{title} {body}".lower()
        hit = [p for p in BANNED if p in blob]
        if hit:
            out.append(f"banned phrase: {hit[0]!r}")
        stale = STALE.search(f"{title} {body}")
        if stale:
            out.append(f"dates the pin: {stale.group(0)!r}")
        if "!" in title or "!" in body:
            out.append("exclamation mark")
        if "#" in blob:
            out.append("hashtag in the copy")
        return out

    @classmethod
    def _parse(cls, raw: str) -> Optional[Dict[str, str]]:
        match = re.search(r"\{.*\}", raw or "", re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        return {k: str(data.get(k, "")).strip()
                for k in ("title", "body", "photo")}

    # ── writing ──────────────────────────────────────────────────

    async def write(self, board: str,
                    avoid: Optional[List[str]] = None) -> Optional[Dict]:
        """
        One fresh tip for this board, or None.

        None means nothing publishable came back. The caller falls through to
        the hand-written bank rather than publishing something weaker -- an
        advice pin's whole job is being worth saving.
        """
        raw = await self.ai.generate(
            task="social_caption",
            system_prompt=SYSTEM,
            user_prompt=self._user_prompt(board, avoid or []),
            max_tokens=500,
            # High, deliberately. These pins run for months and the fastest
            # way to look automated is forty variations of one sentence.
            temperature=0.9,
            validator=self._looks_like_json,
            min_attempts=3,
        )
        if not raw:
            self.last_error = "the model returned nothing usable"
            logger.warning(f"No tip written for '{board}'.")
            return None

        tip = self._parse(raw)
        if not tip:
            self.last_error = "the answer did not parse as JSON"
            logger.warning(f"Tip for '{board}' did not parse.")
            return None

        problems = self._problems(tip)
        if problems:
            self.rejected += 1
            self.last_error = "; ".join(problems)
            logger.info(f"Tip rejected for '{board}': {self.last_error} "
                        f"-- {tip.get('title', '')[:48]}")
            return None

        if board not in board_routing.ALL_BOARDS:
            logger.warning(f"'{board}' is not a real board; using the default.")
            board = board_routing.DEFAULT_BOARD

        self.written += 1
        logger.info(f"Tip written for '{board}': {tip['title']}")
        return {"board": board, "title": tip["title"], "body": tip["body"],
                "photo": tip["photo"], "credit": "", "image": ""}

    # ── board rotation ───────────────────────────────────────────

    @staticmethod
    def pick_board(recent_boards: Optional[List[str]] = None) -> str:
        """
        The board with the least attention lately.

        Pinterest distributes by board, so feeding one and starving five
        wastes most of the profile. Least-recently-used rather than random,
        because a random draw leaves a board empty for a fortnight often
        enough to matter on an account this size.
        """
        recent = list(recent_boards or [])
        counts = {b: recent.count(b) for b in board_routing.ALL_BOARDS}
        fewest = min(counts.values())
        return random.choice([b for b, n in counts.items() if n == fewest])

    @property
    def status(self) -> Dict:
        return {"written": self.written, "rejected": self.rejected,
                "last_error": self.last_error}
