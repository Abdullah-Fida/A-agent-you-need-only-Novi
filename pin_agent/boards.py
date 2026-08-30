"""
Which board a pin belongs on.

Pinterest distributes pins by board topic, so the board is the strongest signal
for who gets shown the pin. Dropping everything into one board wastes that and
makes the profile read as a dumping ground.

Routing is on board NAME, not id. Pinterest board ids change whenever the
channel is disconnected and reconnected in Buffer -- which happened once
already -- and a routing table full of stale numbers fails silently by sending
every pin to the fallback. Names are stable and readable.

Pure text matching, no model. Getting this wrong is cosmetic rather than
dangerous, but a model call per pin would cost latency and tokens for a
decision a keyword list makes correctly.
"""
import logging
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("PinAgent.Boards")

# Ordered most specific first. A product matching several boards goes to the
# one with the most hits; ties break towards the earlier, narrower entry.
BOARD_ROUTES: List[Tuple[str, Tuple[str, ...]]] = [
    ("Bathroom Storage Ideas", (
        "bathroom", "shower", "toilet", "vanity", "towel", "toothbrush",
        "soap", "shampoo", "razor", "cosmetic", "makeup",
    )),
    ("Pantry and Fridge Storage", (
        "pantry", "fridge", "refrigerator", "freezer", "airtight", "cereal",
        "spice", "egg holder", "food container", "food storage", "canister",
        "jar", "bottle rack", "lunch", "bread box",
    )),
    ("Under Sink and Cabinet Storage", (
        "under sink", "under-sink", "cabinet", "cupboard", "sliding", "pull out",
        "pull-out", "tension rod", "shelf riser", "riser", "stackable",
        "under shelf", "sink caddy", "sponge holder",
    )),
    ("Tiny Apartment Solutions", (
        "closet", "wardrobe", "over door", "over-door", "wall hook", "wall mount",
        "wall-mount", "hanging", "foldable", "folding", "collapsible",
        "space saving", "space-saving", "apartment", "hook", "shoe rack",
    )),
    ("Kitchen Gadgets Worth Buying", (
        "scissors", "shears", "opener", "peeler", "grater", "measuring",
        "gadget", "cutter", "slicer", "whisk", "strainer", "colander", "masher",
        "tongs", "chopper", "press", "thermometer", "timer", "brush",
    )),
    ("Small Kitchen Organization", (
        "drawer", "utensil", "counter", "countertop", "worktop", "dish rack",
        "cutlery", "knife block", "pot lid", "pan organizer", "corner shelf",
        "kitchen rack", "kitchen shelf", "organizer", "divider",
    )),
]

# Where anything unmatched goes. The broadest board of the six.
DEFAULT_BOARD = "Small Kitchen Organization"

ALL_BOARDS = [name for name, _ in BOARD_ROUTES]


def choose_board(*texts: str) -> str:
    """
    The board name for a product, from its title, category and search term.

    Pass every scrap of text available -- the listing title is keyword soup and
    the useful word is as likely to be in the category or the search term that
    found it.
    """
    haystack = " ".join(t for t in texts if t).lower()
    if not haystack.strip():
        return DEFAULT_BOARD

    best_name, best_score = DEFAULT_BOARD, 0
    for name, keywords in BOARD_ROUTES:
        # Bounded at BOTH ends. Anchoring only the start lets "jar" fire on
        # "jarring" and "counter" on "counterfeit", which misroutes silently.
        # The optional suffix keeps ordinary inflections working, so
        # "wall mount" still catches "wall mounted".
        score = sum(1 for kw in keywords
                    if re.search(r"\b" + re.escape(kw) + r"(?:s|es|ed|ing)?\b",
                                 haystack))
        if score > best_score:
            best_name, best_score = name, score

    if best_score:
        logger.debug(f"Routed to '{best_name}' ({best_score} keyword hits).")
    return best_name


def resolve(name: str, board_ids: Dict[str, str]) -> Optional[str]:
    """
    Turns a board name into the id Pinterest knows it by.

    `board_ids` maps name -> serviceId, read from the live channel. A name that
    is not there means the board was renamed or deleted on Pinterest, so this
    returns None and lets the caller fall back rather than publishing to
    whatever happens to sort first.
    """
    if not board_ids:
        return None
    if name in board_ids:
        return board_ids[name]

    lowered = {k.lower(): v for k, v in board_ids.items()}
    return lowered.get((name or "").lower())
