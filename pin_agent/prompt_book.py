"""
The twenty-eight day schedule: which pin goes out, on which board, when.

READ FROM THE TEXT FILE, not copied into code. pin_prompts_28_days.txt is
the thing a person edits -- it is laid out to be read, it explains itself,
and it is where a title gets reworded at nine in the evening. Parsing it
means the file IS the schedule rather than a document about the schedule
that quietly drifts out of date.

WHAT A DAY LOOKS LIKE
Six advice pins, one per board, plus one affiliate pin sourced live from
AliExpress. The six are fixed in advance: board, title and scene are all
decided, so nothing is left to a model except the picture and the
description.

WHICH DAY IS TODAY
Counted from a fixed epoch and wrapped at twenty-eight, so the cycle turns
on its own and a restart lands on the same day it left. Not stored, because
anything stored can disagree with the calendar after an outage.

WHY FIXED RATHER THAN CHOSEN
The bot used to pick a tip at random from a bank, and that is what produced
a fortnight of near-repeats: nothing stopped it choosing the same subject on
Tuesday that it chose on Sunday. A schedule cannot repeat itself. It also
means the week hangs together as a set, because the file groups a style to
a week.
"""
import logging
import os
import re
from datetime import date
from typing import Dict, List, Optional

logger = logging.getLogger("PinAgent.PromptBook")

CYCLE_DAYS = 28
PINS_PER_DAY = 6          # one per board; the affiliate pin is separate

# Day one of the cycle. Any fixed past Monday does; this is the Monday the
# schedule was written.
EPOCH = date(2026, 9, 21)

# Searched in order. The file lives beside the project rather than inside
# it, so it can be edited without touching the repo.
SEARCH = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "pin_prompts_28_days.txt"),
    os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "pin_prompts_28_days.txt"),
    "pin_prompts_28_days.txt",
]

_ENTRY = re.compile(
    r"^(?P<day>\d+)\.(?P<slot>\d)\s+(?P<board>[A-Z][A-Z ]+?)\s*\n"
    r"\s+Title:\s+(?P<title>.+?)\s*\n"
    r"(?:\s+Overlay:\s+(?P<overlay>.+?)\s*\n)?"
    r"\s+Scene:\s+(?P<scene>.*?)(?=\n\s*\n)", re.M | re.S)

# WORDS IN THE PICTURE, when a pin actually needs them.
#
# Almost none do. The pin is a photograph now -- nothing is drawn over it --
# and Pinterest shows the title beside it in its own type, so a caption
# burned into the image is a second headline arguing with the first.
#
# When a pin does need a word or two, the model paints them INTO the scene:
# chalked on a board, printed on a jar label. That reads as something in the
# room rather than a caption stuck on top, and it is the only kind of text
# worth having on a photograph.
#
# Add an "Overlay:" line to an entry in the file to turn it on. Two or three
# words. Longer than that and it is a caption again.
OVERLAY_WORDS = 3

_ASK_FOR_WORDS = (
    "One exception to the rule above: the words \"{words}\" SHOULD appear in "
    "the image, and nothing else may. Paint them into the scene as part of "
    "it -- chalked on a small board, printed on a label, lettered on a jar -- "
    "so they belong to the room rather than sitting on top of the "
    "photograph. Spell them exactly as written, in a clean simple hand."
)

_STYLE = re.compile(r"STYLE BLOCK \(weeks? \d[^)]*\)\s*\n\s*\n(.*?)\n\s*\n-{5}",
                    re.S)

# BOARD NAMES ARE SHOUTED IN THE FILE because it is read by eye. The agent
# needs the real ones.
_BOARDS = {
    "BATHROOM STORAGE IDEAS": "Bathroom Storage Ideas",
    "PANTRY AND FRIDGE STORAGE": "Pantry and Fridge Storage",
    "UNDER SINK AND CABINET STORAGE": "Under Sink and Cabinet Storage",
    "TINY APARTMENT SOLUTIONS": "Tiny Apartment Solutions",
    "KITCHEN GADGETS WORTH BUYING": "Kitchen Gadgets Worth Buying",
    "SMALL KITCHEN ORGANIZATION": "Small Kitchen Organization",
}


class PromptBook:
    """The schedule, loaded once."""

    def __init__(self, path: str = ""):
        self.path = path or self._find()
        self.tail = ""
        self.styles: List[str] = []
        self.days: Dict[int, List[Dict]] = {}
        self.error = ""
        if self.path:
            self._load()

    @staticmethod
    def _find() -> str:
        for candidate in SEARCH:
            if os.path.exists(candidate):
                return candidate
        return ""

    @property
    def is_ready(self) -> bool:
        return len(self.days) == CYCLE_DAYS and bool(self.tail)

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            self.error = f"could not read {self.path}: {e}"
            logger.error(self.error)
            return

        # The tail runs from the rule under its heading to the next banner.
        # Matched rather than split on a fixed number of dashes, because the
        # rules in the file are 79 characters long and splitting on a
        # shorter run produced empty pieces and an empty tail.
        tail = re.search(r"FIXED TAIL[^\n]*\n-{10,}\n(.*?)\n={10,}",
                         text, re.S)
        self.tail = tail.group(1).strip() if tail else ""

        self.styles = [" ".join(s.split()) for s in _STYLE.findall(text)]

        for m in _ENTRY.finditer(text):
            board = _BOARDS.get(m.group("board").strip())
            if not board:
                continue
            day = int(m.group("day"))
            overlay = " ".join((m.group("overlay") or "").split())
            if overlay and len(overlay.split()) > OVERLAY_WORDS:
                # Longer than a few words is a caption, and a caption on a
                # photograph is the thing this was built to stop.
                logger.warning(f"Overlay on day {day} is too long, ignoring: "
                               f"{overlay!r}")
                overlay = ""
            self.days.setdefault(day, []).append({
                "day": day,
                "slot": int(m.group("slot")),
                "board": board,
                "title": m.group("title").strip(),
                "scene": " ".join(m.group("scene").split()),
                "overlay": overlay,
            })

        for day in self.days:
            self.days[day].sort(key=lambda e: e["slot"])

        short = [d for d, v in self.days.items() if len(v) != PINS_PER_DAY]
        if short:
            self.error = f"days with the wrong number of pins: {short[:5]}"
            logger.error(self.error)
        elif len(self.days) != CYCLE_DAYS:
            self.error = f"found {len(self.days)} days, expected {CYCLE_DAYS}"
            logger.error(self.error)
        else:
            logger.info(f"Prompt book loaded: {len(self.days)} days, "
                        f"{sum(len(v) for v in self.days.values())} pins, "
                        f"{len(self.styles)} styles.")

    # ── which day, and which pin ─────────────────────────────────

    @staticmethod
    def day_number(when: Optional[date] = None) -> int:
        """
        1..28. Wraps on its own, so the cycle never has to be reset.
        """
        when = when or date.today()
        return ((when - EPOCH).days % CYCLE_DAYS) + 1

    def week_style(self, when: Optional[date] = None) -> str:
        """The style block that applies to this day's week."""
        if not self.styles:
            return ""
        week = (self.day_number(when) - 1) // 7
        return self.styles[week % len(self.styles)]

    def for_day(self, when: Optional[date] = None) -> List[Dict]:
        """Today's six, in board order."""
        return list(self.days.get(self.day_number(when), []))

    def prompt_for(self, entry: Dict, when: Optional[date] = None) -> str:
        """
        The full picture prompt: the week's style, the scene, the tail.

        Exactly the three pieces the file says to paste together, in that
        order, so what ships is what was written and reviewed.
        """
        parts = [self.week_style(when), entry.get("scene", ""), self.tail]

        # The tail forbids text outright, which is right for almost every
        # pin. When an entry asks for a word or two, the exception is added
        # AFTER it so the model reads the rule and then the one carve-out,
        # rather than a contradiction it has to guess its way through.
        overlay = (entry.get("overlay") or "").strip()
        if overlay:
            parts.append(_ASK_FOR_WORDS.format(words=overlay))

        return "\n\n".join(p for p in parts if p)

    @property
    def status(self) -> Dict:
        return {
            "ready": self.is_ready,
            "file": os.path.basename(self.path) if self.path else "",
            "days": len(self.days),
            "pins": sum(len(v) for v in self.days.values()),
            "styles": len(self.styles),
            "today": self.day_number(),
            "error": self.error,
        }
