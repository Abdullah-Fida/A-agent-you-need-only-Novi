"""
What KIND of thing a pin is about.

WHY THIS EXISTS.
The owner kept seeing the same pin on the live board. An exact comparison
found nothing -- no repeated title, no repeated image, no repeated link. What
was actually happening is worse and harder to spot in a database: the same
PRODUCT TYPE, over and over. Nine of fifty-five pins were spice racks. Seven
of them inside twelve days:

    02 Sep  Clear Your Counter with a Pull-Out Spice Drawer
    02 Sep  Space-saving stretchable spice rack for tiny kitchens
    03 Sep  This 4-layer adjustable spice drawer organizer ...
    06 Sep  Compact spice organizer keeps jars tidy in small kitchens
    12 Sep  Keep your kitchen clear with a spice drawer organizer
    13 Sep  Stop Jumbled Spices with Easy Drawer Organizer

Every one is a different listing with a different link and a different
photograph, so every existing guard passed it. To a person scrolling the
board it reads as one pin posted six times.

The title guard could not catch it either. After the thirty-two hour outage
the noise list was widened to include "drawer", "organizer", "rack" and
"clear" -- the words that were making unrelated pins look alike. That fixed
the outage and opened this: "Clear Your Counter with a Pull-Out Spice Drawer"
reduces to {pull, spice} and "Keep your kitchen clear with a spice drawer
organizer" reduces to {spice}. One word in common, and the bar is three.

So identity is asked as a separate question from wording. Two pins about a
spice rack are the same subject however differently they are written.

DELIBERATELY NOT A MODEL. A language model asked "are these the same kind of
thing?" is right most of the time, and the cost of the times it is wrong is
the board looking automated. Keywords are testable and they do not drift.
"""
import logging
import re
from typing import List, Optional, Tuple

logger = logging.getLogger("PinAgent.Types")

# Ordered MOST SPECIFIC FIRST. "spice drawer organizer" has to resolve to
# spice_rack rather than drawer_organizer, so spice_rack is listed above it.
#
# The canonical name on the left is what gets stored and what the cooldown
# counts. Keep it stable -- renaming one resets its cooldown.
TYPE_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("spice_rack", ("spice", "spices", "seasoning", "herb jar", "masala")),
    ("egg_holder", ("egg holder", "egg tray", "egg box", "egg container",
                    "egg organizer", "eggs")),
    ("cutlery_tray", ("cutlery", "flatware", "silverware", "utensil tray",
                      "utensil holder", "knife block")),
    ("shoe_storage", ("shoe", "shoes", "boot", "slipper", "sneaker")),
    ("towel_rack", ("towel", "towels", "bath sheet", "hand cloth")),
    ("toothbrush_holder", ("toothbrush", "tooth brush", "toothpaste")),
    ("soap_dish", ("soap", "shampoo", "body wash", "dispenser")),
    # "sink" alone, and listed above rolling_cart on purpose: "Compact
    # pull-out organizer for tiny kitchen sinks" matched "pull-out" and
    # came back as a trolley.
    ("under_sink", ("under sink", "under-sink", "undersink", "sink caddy",
                    "sink organizer", "sink shelf", "sink", "sinks")),
    ("fridge_bin", ("fridge", "refrigerator", "freezer")),
    ("pantry_container", ("cereal", "airtight", "food container",
                          "food storage", "canister", "grain", "flour",
                          "sugar container", "pantry")),
    ("pot_rack", ("pot lid", "pan organizer", "pot organizer", "pots",
                  "pans", "saucepan", "cookware", "lid rack", "lid holder")),
    ("closet_organizer", ("closet", "wardrobe", "underwear", "sock",
                          "clothes", "hanger", "garment")),
    ("door_organizer", ("over door", "over-door", "door hanging",
                        "hanging organizer", "door rack", "back of door")),
    ("rolling_cart", ("rolling cart", "trolley", "utility cart", "wheeled",
                      "cart with wheels", "slide out", "pull out",
                      "pull-out")),
    ("wall_shelf", ("wall shelf", "floating shelf", "corner shelf",
                    "wall mount", "wall-mount", "wall rack", "acrylic shelf")),
    ("shelf_riser", ("riser", "shelf insert", "stackable shelf",
                     "cabinet shelf", "expandable shelf")),
    ("hook_rail", ("hook", "hooks", "rail", "peg", "hanger strip")),
    ("storage_basket", ("basket", "wicker", "woven bin", "fabric bin")),
    ("storage_box", ("storage box", "storage boxes", "storage bin",
                     "storage container", "lidded box", "cube")),
    ("drawer_organizer", ("drawer divider", "drawer organizer",
                          "drawer tray", "drawer insert", "drawer")),
    ("cabinet_organizer", ("cabinet", "cupboard", "pantry shelf")),
    ("desk_organizer", ("desk", "stationery", "pen holder", "office")),
    ("dish_rack", ("dish rack", "drying rack", "draining", "dish drainer")),
    ("cleaning_caddy", ("cleaning", "sponge", "mop", "broom", "bucket",
                        "spray bottle", "duster")),
    ("bag_holder", ("bag holder", "bin bag", "trash", "garbage", "rubbish",
                    "waste bin")),
    ("jar_set", ("jar", "jars", "bottle", "bottles")),
    ("shelf_unit", ("shelf", "shelves", "shelving", "rack", "tier",
                    "layer", "stand")),
]

# Last resort when nothing matches. Kept distinct so the cooldown still
# applies to it -- three "unknown" pins in a week are as repetitive as
# three spice racks, even if nothing here could name them.
FALLBACK = "general_organizer"

# A type may return after this long.
#
# SEVEN, measured rather than chosen. Replayed against the 47 pins that
# actually published, the window makes almost no difference past a week --
# 7 days blocks 22 of them, 10 blocks 23, 14 blocks 25 -- while the risk of
# starving the agent climbs the whole way. Seven catches the repetition a
# person notices and leaves the pool wide enough to pick from.
#
# And nothing is lost when it does block: a product slot with no fresh
# subject falls through to an advice pin, which never repeats and improves
# the affiliate ratio. That fallback is what makes a strict rule safe here,
# and it did not exist when the duplicate guard caused a 32-hour outage.
COOLDOWN_DAYS = 7


def _matches(haystack: str, keyword: str) -> bool:
    """Whole words only. Unbounded, 'jar' fires on 'jarring' and 'pot' on
    'spotted', which is how a corner shelf became a pot rack."""
    return re.search(r"\b" + re.escape(keyword) + r"(?:s|es|ed|ing)?\b",
                     haystack) is not None


def classify(*texts: str) -> str:
    """
    The canonical product type for a pin.

    PASS THE TITLE, NOT THE DESCRIPTION. Descriptions mention other things
    in passing -- "keeping utensils, spices, and small kitchen tools tidy"
    is a drawer box, but the word "spices" is in it. Classifying on the body
    text put a rolling cart, a basket, a pair of scissors and a set of
    storage boxes all in the spice bucket, and would have blocked 39 of 55
    pins instead of the 22 that are genuinely repetitive.

    Returns the FIRST rule that matches, not the best-scoring one, because
    the list is ordered specific-to-general on purpose: a spice drawer
    organiser is a spice rack first and a drawer organiser second.
    """
    haystack = " ".join(t for t in texts if t).lower()
    if not haystack.strip():
        return FALLBACK

    for name, keywords in TYPE_RULES:
        if any(_matches(haystack, kw) for kw in keywords):
            return name
    return FALLBACK


def describe(product_type: str) -> str:
    """The type as a person would say it, for log lines and emails."""
    return (product_type or FALLBACK).replace("_", " ")


def blocked_by_cooldown(product_type: str,
                        recent_types: Optional[List[str]] = None) -> bool:
    """
    Whether this subject has been pinned too recently.

    `recent_types` is what the store returns for the cooldown window, so the
    window itself is decided in one place and this only answers the
    question.
    """
    if not product_type:
        return False
    return product_type in set(recent_types or ())


ALL_TYPES = [name for name, _ in TYPE_RULES] + [FALLBACK]
