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

    # Words that appear in every tip in this niche and so identify nothing.
    _EMPTY = {
        "the", "a", "an", "and", "or", "with", "for", "your", "you", "keep",
        "put", "use", "place", "hang", "store", "small", "little", "one",
        "two", "into", "onto", "from", "that", "this", "them", "they", "it",
        "its", "in", "on", "of", "to", "is", "are", "up", "out", "off", "at",
        "by", "so", "can", "will", "not", "top", "back", "front", "side",
        "away", "more", "less", "than", "then", "when", "where", "what",
        "space", "tidy", "neat", "clean", "clear", "organise", "organize",
        "storage", "home", "house", "room", "keeps", "hold", "holds",
    }

    @classmethod
    def _objects_taken(cls, avoid: List[str], limit: int = 20) -> List[str]:
        """
        The physical things recent tips were ABOUT.

        Handing the model a list of sentences is not enough. Told to avoid
        forty-five titles it produced FIVE separate tips about magnetic knife
        strips -- "Place a magnetic strip on the wall to hold knives
        upright", "Hang knives on a magnetic strip beside the cutting board",
        "Mount a magnetic strip for knives on the kitchen wall" -- each
        worded differently, every one a duplicate. It avoids the sentences
        and fixates on the idea. Naming the OBJECTS is what moves it on.
        """
        seen, out = set(), []
        for title in (avoid or [])[-40:]:
            for w in re.findall(r"[a-z]{3,}", (title or "").lower()):
                if w in cls._EMPTY or w in seen:
                    continue
                seen.add(w)
                out.append(w)
        return out[-limit:]

    @classmethod
    def _user_prompt(cls, board: str, avoid: List[str]) -> str:
        recent = "\n".join(f"- {t}" for t in (avoid or [])[-10:])
        taken = cls._objects_taken(avoid)
        blocks = []
        if taken:
            blocks.append(
                "ALREADY COVERED. Do not write about any of these, or "
                "anything close to them. Pick a different object in a "
                "different part of the room:\n" + ", ".join(taken))
        if recent:
            blocks.append("Recent tips, whose wording you must not reuse:\n"
                          + recent)
        body = ("\n\n" + "\n\n".join(blocks)) if blocks else ""
        return (f"Write one tip for a Pinterest board called "
                f"\"{board}\".{body}")

    # ── validation ───────────────────────────────────────────────

    @classmethod
    def _is_publishable(cls, text: str) -> bool:
        """
        The FULL check, run as the engine's validator rather than after it.

        Checking only the shape and validating afterwards threw away one
        answer in four -- almost always for a title a few characters over or
        under -- and every one of those was a wasted call that fell back to
        the bank. Validating inside the loop lets the engine simply ask
        again, which is what it is for.
        """
        if not (text and "{" in text and "}" in text and '"title"' in text):
            return False
        tip = cls._parse(text)
        return bool(tip) and not cls._problems(tip)

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

    @staticmethod
    def _tidy(text: str) -> str:
        """
        Capitalise the opening letter and nothing else.

        The model returns a lowercase first word perhaps one time in seven
        -- "use a pull-out knife tray", "hang loofahs on wall hooks" -- and
        on a pin, where the title is set in 56px bold, that reads as a
        mistake. Only the first character is touched: "lazy Susan" and any
        other proper noun the model got right must survive.
        """
        text = (text or "").strip()
        return text[:1].upper() + text[1:] if text else text

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
        out = {k: str(data.get(k, "")).strip()
               for k in ("title", "body", "photo")}
        out["title"] = cls._tidy(out["title"])
        out["body"] = cls._tidy(out["body"])
        return out

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
            validator=self._is_publishable,
            min_attempts=4,
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

    async def write_for_photo(self, board: str, description: str,
                              avoid: Optional[List[str]] = None
                              ) -> Optional[Dict]:
        """
        A tip written to suit a photograph that has already been found.

        THIS IS THE RIGHT WAY ROUND, and it took three wrong pins to see it.
        Writing the tip first means hunting for a picture of that exact
        thing, and the open libraries simply do not hold one for every
        specific idea -- there is no photograph of a tension rod holding
        spray bottles under a sink. So the search broadens, finds something
        generic, and the match becomes luck: a tip about magnetic knife
        strips was illustrated with a roll of camera film, because the
        broadened query was "counter" and the photograph had a counter in
        it.

        Starting from the photograph removes the problem rather than
        filtering it. Broad household subjects -- a tidy worktop, an open
        pantry, a bathroom shelf -- are plentiful and well photographed, and
        a tip written to suit what is actually in the picture always suits
        it.
        """
        rules = (
            f"Here is a photograph that will be the pin's picture:\n"
            f"  \"{description}\"\n\n"
            f"Write a home organisation tip for the board \"{board}\" that "
            f"this photograph illustrates. The tip must make sense to "
            f"someone looking at that exact picture -- write about what is "
            f"IN it, not about something it reminds you of. Do not describe "
            f"the photograph; give advice."
        )
        taken = self._objects_taken(avoid or [])
        if taken:
            rules += ("\n\nAlready covered, so choose a different angle on "
                      "the picture if any of these fit it:\n" + ", ".join(taken))

        raw = await self.ai.generate(
            task="social_caption", system_prompt=SYSTEM, user_prompt=rules,
            max_tokens=500, temperature=0.85,
            validator=self._is_publishable, min_attempts=4)
        if not raw:
            self.last_error = "the model returned nothing usable"
            return None

        tip = self._parse(raw)
        if not tip:
            self.last_error = "the answer did not parse as JSON"
            return None
        problems = self._problems(tip)
        if problems:
            self.rejected += 1
            self.last_error = "; ".join(problems)
            return None

        if board not in board_routing.ALL_BOARDS:
            board = board_routing.DEFAULT_BOARD
        self.written += 1
        logger.info(f"Tip written for a photograph on '{board}': "
                    f"{tip['title']}")
        # `photo` is unused on this path -- the picture is already chosen --
        # but it is kept so the two paths return the same shape.
        return {"board": board, "title": tip["title"], "body": tip["body"],
                "photo": tip.get("photo", ""), "credit": "", "image": ""}

    # ── board rotation ───────────────────────────────────────────

    # Broad household subjects the open libraries are actually well stocked
    # with, per board. Deliberately general: a photograph of "a tidy kitchen
    # worktop" exists in thousands, a photograph of "a tension rod holding
    # spray bottles under a sink" does not exist at all. The tip is written
    # to whatever comes back, so the search never has to be specific.
    PHOTO_SUBJECTS = {
        "Bathroom Storage Ideas": (
            "bathroom interior", "bathroom shelf", "bathroom sink",
            "bathroom towels", "shower", "bathroom cabinet"),
        "Pantry and Fridge Storage": (
            "pantry shelves", "kitchen pantry", "open refrigerator",
            "food jars", "kitchen storage jars", "spice jars"),
        "Under Sink and Cabinet Storage": (
            "kitchen sink", "kitchen cabinet", "cleaning supplies",
            "kitchen cupboard", "cleaning bottles", "kitchen sponge"),
        "Tiny Apartment Solutions": (
            "small apartment interior", "closet clothes", "wardrobe",
            "entryway hallway", "bedroom interior", "shoe storage"),
        "Kitchen Gadgets Worth Buying": (
            "kitchen utensils", "kitchen knife", "kitchen scale",
            "cutting board", "measuring spoons", "kitchen timer"),
        "Small Kitchen Organization": (
            "kitchen worktop", "kitchen drawer", "kitchen shelf",
            "home kitchen counter", "kitchen interior", "pots and pans"),
    }

    @classmethod
    def photo_subject(cls, board: str, avoid: Optional[List[str]] = None):
        """A well-stocked search subject for this board, least used first."""
        subjects = cls.PHOTO_SUBJECTS.get(board) or ("home interior",)
        recent = list(avoid or [])
        counts = {s: recent.count(s) for s in subjects}
        fewest = min(counts.values())
        return random.choice([s for s, n in counts.items() if n == fewest])

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
