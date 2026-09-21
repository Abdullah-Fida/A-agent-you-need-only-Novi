"""
A different look every week, and no two pins asked for the same way.

TWO PROBLEMS, ONE FILE.

A board where every pin is lit the same, shot from the same height, in the
same kitchen, reads as a template within a fortnight -- and a template is
the clearest possible signal that nobody is behind it. So the STYLE turns
over weekly: one week is bright Scandinavian, the next is warm rustic, the
next is quiet Japandi. Seven pins in a row hang together as a set, which is
what makes a profile look considered, and the set changes before anyone
tires of it.

And within a week the prompt still has to vary, or the same style produces
the same photograph over and over. Four axes move independently -- the hour
of the light, the height of the camera, how close it stands, and one
incidental human detail -- which is 4 x 4 x 4 x 8 combinations before the
style is even chosen.

WHY THE WEEK AND NOT RANDOM. A random style per pin gives a feed that looks
like four different magazines every day. The week is the unit a viewer
perceives as "this account's look right now".
"""
import hashlib
from datetime import date
from typing import Dict, List, Optional, Tuple

# ── the looks, one per week ──────────────────────────────────────
#
# Each is a real interiors idiom rather than a filter name, because the
# model answers to the vocabulary of photography and design, not to
# adjectives. Palette, light and materials are named so the whole week
# holds together even across different rooms.
STYLES: List[Dict[str, str]] = [
    {
        "name": "Scandinavian daylight",
        "look": "Scandinavian interior style: white walls, pale oak, "
                "unpainted linen, a single trailing green plant",
        "light": "cool bright overcast daylight through a large window, "
                 "soft shadows, airy and high-key",
        "finish": "clean matte surfaces, nothing glossy",
    },
    {
        "name": "Warm rustic farmhouse",
        "look": "English farmhouse kitchen: aged solid wood, cream painted "
                "cabinetry, stoneware and enamel, a worn butcher's block",
        "light": "low golden late-afternoon sun raking across the room, "
                 "long warm shadows",
        "finish": "honest worn textures, visible grain and patina",
    },
    {
        "name": "Quiet Japandi",
        "look": "Japandi: muted greige and charcoal, black steel, pale ash, "
                "handmade ceramics, deliberate empty space",
        "light": "diffused north light, very soft gradients, restrained",
        "finish": "matte, tactile, nothing shiny or decorative",
    },
    {
        "name": "Sunlit Mediterranean",
        "look": "Mediterranean home: lime-washed plaster, terracotta, "
                "olive green, woven rush, hand-glazed tile",
        "light": "hard bright midday sun through a shutter, crisp shadow "
                 "shapes on the wall",
        "finish": "chalky plaster and unglazed clay",
    },
    {
        "name": "Modern monochrome",
        "look": "contemporary city flat: soft white, graphite grey, brushed "
                "stainless, smoked glass, one black accent",
        "light": "even soft studio-like daylight, gentle falloff, "
                 "architectural",
        "finish": "precise edges, seamless cabinetry, no visible handles",
    },
    {
        "name": "Coastal linen",
        "look": "coastal cottage: chalk white, pale sand, faded indigo, "
                "rattan and rope, washed floorboards",
        "light": "bright hazy sea light, slightly blown highlights at the "
                 "window, fresh",
        "finish": "sun-bleached, softly worn paint",
    },
    {
        "name": "Mid-century walnut",
        "look": "mid-century modern: walnut and teak, mustard and olive "
                "accents, tapered legs, brass hardware",
        "light": "warm directional afternoon light, rich saturated colour",
        "finish": "oiled wood with a satin sheen, warm metals",
    },
    {
        "name": "Soft botanical",
        "look": "a plant-filled room: sage and cream, unglazed pots, "
                "eucalyptus and trailing pothos, cane furniture",
        "light": "dappled light filtered through leaves, gentle and green-"
                 "tinged",
        "finish": "natural fibre, terracotta, nothing synthetic",
    },
]

# ── the axes that move WITHIN a week ─────────────────────────────

HOURS = [
    "early morning light",
    "mid-morning light",
    "late afternoon light",
    "soft light just before dusk",
]

CAMERA = [
    "shot at eye level, straight on",
    "shot from slightly above, looking down at about thirty degrees",
    "shot low, close to the surface, looking along it",
    "a three-quarter angle from one side",
]

DISTANCE = [
    "a close detail filling the frame",
    "a tight crop on the arrangement itself",
    "a medium shot showing the piece of furniture and a little of the room",
    "a wider view placing it in the room",
]

# One human trace each time, which is the difference between a photograph
# of a home and a photograph of a showroom.
LIVED_IN = [
    "a folded linen cloth left to one side",
    "a mug of tea half finished",
    "an open cookbook",
    "a small bunch of herbs on the side",
    "a pair of reading glasses set down",
    "a wooden spoon resting where it was used",
    "a stack of clean folded tea towels",
    "a bowl of lemons",
]

# What every picture gets, whatever the week. The refusals matter as much as
# the requests: generated lettering is always misspelled, and the pin
# template prints the title across the lower third itself.
CRAFT = (
    "Editorial interior photography for a premium home magazine. "
    "Photorealistic, sharp, high detail, professionally styled and "
    "immaculately clean. Shot on a full-frame camera with a 35mm lens at "
    "f/2.8, natural light only, true-to-life colour, no colour cast."
)

FORBID = (
    "No text, no words, no letters, no numbers, no labels with writing, no "
    "logos, no watermarks, no signage anywhere in the image. No people, no "
    "hands, no faces. Not an illustration, not a 3D render, not a collage."
)

# WHAT ACTUALLY SURVIVES IS THE MIDDLE. The template gives the photograph
# the top 1000x1080 of a 1000x1500 pin and prints the title in a band
# underneath, and a tall picture is scaled to fill that box and centre-
# cropped -- so roughly the top and bottom sixth of what the model draws is
# thrown away, and what is left is close to square.
#
# The first version of this asked for a calm, uncluttered lower third so
# text could sit there. That was wrong twice over: the text is not on the
# photograph at all, and the third being kept clear was the third being
# cropped off. It was asking for a sixth of the picture to be wasted.
SHAPE = ("Vertical portrait orientation, 2:3. Compose the subject in the "
         "CENTRE of the frame and fill it -- the very top and bottom edges "
         "will be cropped away, so nothing important belongs there.")


def style_for(when: Optional[date] = None) -> Dict[str, str]:
    """
    This week's look.

    Keyed on the ISO week so it turns over every Monday on its own, and so
    the same week always gives the same answer -- which means a pin can be
    re-rendered tomorrow and still match the ones beside it.
    """
    when = when or date.today()
    year, week, _ = when.isocalendar()
    return STYLES[(year * 53 + week) % len(STYLES)]


def _spread(seed: str, options: List[str], salt: str) -> str:
    """
    Pick one, evenly and repeatably, from the scene's own words.

    Deterministic rather than random on purpose: the same tip asked twice
    produces the same picture, so a retry after a crash does not quietly
    change the board.
    """
    digest = hashlib.sha256((salt + "|" + seed).encode("utf-8")).digest()
    return options[digest[0] % len(options)]


def compose(scene: str, when: Optional[date] = None,
            avoid: Optional[List[str]] = None) -> Tuple[str, str]:
    """
    The prompt for one picture, and a fingerprint of how it was framed.

    The fingerprint is what stops two pins in a row being the same
    photograph of different objects: the caller keeps the recent ones and
    passes them in, and a collision nudges the framing rather than the
    subject.
    """
    style = style_for(when)
    avoid = set(avoid or [])

    hour = _spread(scene, HOURS, "hour")
    camera = _spread(scene, CAMERA, "camera")
    distance = _spread(scene, DISTANCE, "distance")
    detail = _spread(scene, LIVED_IN, "detail")

    # If this framing has just been used, walk each axis on by one. Walking
    # rather than re-rolling keeps it deterministic.
    for bump in range(1, 5):
        fingerprint = "%s/%s/%s" % (hour, camera, distance)
        if fingerprint not in avoid:
            break
        hour = HOURS[(HOURS.index(hour) + bump) % len(HOURS)]
        camera = CAMERA[(CAMERA.index(camera) + bump) % len(CAMERA)]
        distance = DISTANCE[(DISTANCE.index(distance) + bump) % len(DISTANCE)]
        detail = LIVED_IN[(LIVED_IN.index(detail) + bump) % len(LIVED_IN)]

    prompt = (
        f"{CRAFT}\n\n"
        f"Subject: {scene}.\n\n"
        f"Style: {style['look']}.\n"
        f"Light: {style['light']}, {hour}.\n"
        f"Surfaces: {style['finish']}.\n"
        f"Framing: {distance}, {camera}.\n"
        f"One lived-in detail: {detail}.\n\n"
        f"{SHAPE}\n\n{FORBID}"
    )
    return prompt, "%s/%s/%s" % (hour, camera, distance)
