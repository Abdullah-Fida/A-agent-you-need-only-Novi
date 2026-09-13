"""
Advice pins: the 80% of the feed that sells nothing.

WHY THIS EXISTS.
Every one of the first 41 pins carried an affiliate link. That is the exact
pattern Pinterest suppresses -- the widely reported ratio is 80% useful
content to 20% promotional, and an account where every pin sells something
has its reach cut rather than its content removed. Worse, Pinterest has been
reported since August 2026 to give linked pins far less distribution than
unlinked ones, which fits what this account saw: 41 pins, an audience of
three, and no saves at all.

So these pins carry NO destination link and NO #ad, because there is nothing
to disclose. They are real advice, and their job is to teach Pinterest what
the account is about and earn the distribution the product pins cannot.

Each tip is written to be true without a product. If a tip only makes sense
as a reason to buy something, it belongs in a product pin instead.

EVERY PHOTOGRAPH HERE WAS LOOKED AT BEFORE IT WAS WRITTEN DOWN, and that is
the reason `image` holds a URL rather than the agent running a search at
publish time. Searching live was tried first and it is not good enough for a
live account: "under the bed" returned Nebraska fossil beds, "jar lid" an
Egyptian canopic jar, "hooks on a wall" a brass telescope, and "kitchen
scissors" a Victorian engraving of surgical instruments. Roughly half of
sixty queries came back with something that would have looked broken on the
board. Contact sheets of every candidate were rendered and inspected, the
good ones were kept, and fifteen tips that could not be illustrated properly
were deleted rather than published with a guess.

`photo` is the search term that found the picture. It is kept only so the
choice can be re-examined later; nothing reads it at publish time.

All forty-five are public domain or CC0, so none carries an attribution
obligation -- `credit` is present for the one that eventually does not.
"""
import logging
import random
from typing import Dict, List, Optional

logger = logging.getLogger("PinAgent.Tips")

# Forty-five tips, unevenly spread because the ones that could not be
# illustrated were removed rather than kept with a wrong picture. "Small
# Kitchen Organization" is thinnest at four; it is also the board the product
# pins already feed most heavily, so it needs the least help.
#
# EACH TIP NAMES ITS OWN BOARD rather than being routed by keyword. The
# keyword router in pin_agent.boards reads product titles, and advice does
# not behave like a product title: "Keep two tea towels: one for hands, one
# for dishes" matched "towel" and landed on Bathroom Storage Ideas, and
# "Hooks beat hangers in a narrow hallway" landed on a board about kitchen
# cabinets. Nineteen of the sixty went somewhere other than intended. The
# tips were written a board at a time, so the board is known -- guessing it
# back out of the text afterwards only loses information.
#
# Board names must match pin_agent.boards.ALL_BOARDS exactly; a typo would
# quietly route to the default board, so there is a test on it.
TIP_BANK: List[Dict[str, str]] = [
    # ── Bathroom Storage Ideas ──────────────────────────────────────
    {"board": "Bathroom Storage Ideas",
     "photo": "bathroom shelf",
     "image": "https://pd.w.org/2026/05/686a197e9074a618.71891488-1536x2048.jpg",
     "credit": "",
     "title": "Store bathroom towels rolled, not folded",
     "body": "Rolled towels take about a third less shelf depth than folded "
             "ones, and you can pull one out without collapsing the stack. "
             "It is the plainest way to make a small bathroom cupboard hold "
             "more."},
    {"board": "Bathroom Storage Ideas",
     "photo": "bathroom sink",
     "image": "https://cdn.stocksnap.io/img-thumbs/960w/D3901L7G93.jpg",
     "credit": "",
     "title": "Keep daily bathroom items off the sink edge",
     "body": "Anything that lives on the basin edge gets splashed, and "
             "splashed things need cleaning. Move everyday items to a shelf "
             "or wall rack a few inches up and the basin stays clear with no "
             "extra effort."},
    {"board": "Bathroom Storage Ideas",
     "photo": "shower head",
     "image": "https://images.rawpixel.com/editor_1024/cHJpdmF0ZS9zdGF0aWMvaW1hZ2Uvd2Vic2l0ZS8yMDIyLTA0L2xyL3B4MTMzMDExMy1pbWFnZS1rd3Z3MG9qcC5qcGc.jpg",
     "credit": "",
     "title": "Hang shower bottles upside down to finish them",
     "body": "A bottle stored cap-down empties completely instead of leaving "
             "a quarter behind. A simple wire basket that lets bottles hang "
             "inverted saves more product than any refill trick."},
    {"board": "Bathroom Storage Ideas",
     "photo": "toothbrush",
     "image": "https://cdn.stocksnap.io/img-thumbs/960w/UD9953XS1H.jpg",
     "credit": "",
     "title": "Give your toothbrush somewhere with air to dry",
     "body": "A closed toothbrush holder traps water and stays damp. An open "
             "holder, or a wall clip that lets the head dry, keeps bristles "
             "in better condition for longer."},
    {"board": "Bathroom Storage Ideas",
     "photo": "bathroom cabinet",
     "image": "https://images.rawpixel.com/editor_1024/cHJpdmF0ZS9sci9pbWFnZXMvd2Vic2l0ZS8yMDIyLTA1L3NrNjE1Mi1pbWFnZS1rd3Z4OTJ3dy5qcGc.jpg",
     "credit": "",
     "title": "Sort bathroom storage by how often you reach for it",
     "body": "Daily things at eye level, weekly things below, spares up "
             "high. Most cluttered bathrooms are not short of space -- they "
             "are storing rarely-used items in the easiest places."},
    {"board": "Bathroom Storage Ideas",
     "photo": "bathroom mirror",
     "image": "https://pd.w.org/2022/01/27661f80f3a805477.53307166-1536x2048.jpeg",
     "credit": "",
     "title": "Use the back of the bathroom door for storage",
     "body": "A door is a whole vertical wall nobody uses. Over-door hooks "
             "hold robes, towels and a laundry bag without a single screw, "
             "which matters if you rent."},
    {"board": "Bathroom Storage Ideas",
     "photo": "bar of soap",
     "image": "https://images.rawpixel.com/editor_1024/czNmcy1wcml2YXRlL3Jhd3BpeGVsX2ltYWdlcy93ZWJzaXRlX2NvbnRlbnQvbHIvdXB3azYyMDUzNzg5LXdpa2ltZWRpYS1pbWFnZS1rb3dqaTNoZi5qcGc.jpg",
     "credit": "",
     "title": "Let the soap drain and it lasts twice as long",
     "body": "Soap sitting in its own puddle dissolves from underneath. Any "
             "dish that drains -- slatted, ridged or raised -- roughly "
             "doubles how long a bar survives."},
    {"board": "Bathroom Storage Ideas",
     "photo": "bathroom towels",
     "image": "https://cdn.stocksnap.io/img-thumbs/960w/CTIWKA4B9N.jpg",
     "credit": "",
     "title": "One hook per person beats one rail for everyone",
     "body": "Towels dry far faster spread on separate hooks than overlapped "
             "on a shared rail, and nobody has to work out which one is "
             "theirs."},
    {"board": "Bathroom Storage Ideas",
     "photo": "bathroom interior",
     "image": "https://images.rawpixel.com/editor_1024/czNmcy1wcml2YXRlL3Jhd3BpeGVsX2ltYWdlcy93ZWJzaXRlX2NvbnRlbnQvbHIvZmwxOTY3MDM4MTUwNi1pbWFnZS1reWJlbjl4bi5qcGc.jpg",
     "credit": "",
     "title": "Store cleaning supplies where the mess happens",
     "body": "A cloth and spray kept in the bathroom get used; the same "
             "things kept in a kitchen cupboard do not. Storage near the job "
             "is what makes a small job get done."},
    # ── Pantry and Fridge Storage ───────────────────────────────────
    {"board": "Pantry and Fridge Storage",
     "photo": "pantry shelves",
     "image": "https://images.rawpixel.com/editor_1024/czNmcy1wcml2YXRlL3Jhd3BpeGVsX2ltYWdlcy93ZWJzaXRlX2NvbnRlbnQvbHIvcHg2NzAwNjktaW1hZ2Uta3d5b3cyNmguanBn.jpg",
     "credit": "",
     "title": "Decant pantry staples only if you will refill them",
     "body": "Matching jars look wonderful and are abandoned within weeks if "
             "refilling is awkward. Decant the four or five things you use "
             "constantly and leave the rest in their packets."},
    {"board": "Pantry and Fridge Storage",
     "photo": "spice jars",
     "image": "https://cdn.stocksnap.io/img-thumbs/960w/MZJ54E9D0E.jpg",
     "credit": "",
     "title": "Store spices away from the cooker, not above it",
     "body": "Heat and light strip spices of flavour within months. A drawer "
             "or a shaded cupboard keeps them usable for a year or more, "
             "even though the shelf above the hob is more convenient."},
    {"board": "Pantry and Fridge Storage",
     "photo": "kitchen pantry",
     "image": "https://images.rawpixel.com/editor_1024/cHJpdmF0ZS9sci9pbWFnZXMvd2Vic2l0ZS8yMDIzLTAzL2ZmOTkyLWltYWdlLmpwZw.jpg",
     "credit": "",
     "title": "Put a shallow bin at the front of a deep shelf",
     "body": "Deep shelves lose things at the back permanently. A shallow "
             "container you can pull forward turns the back half into "
             "storage you will actually use."},
    {"board": "Pantry and Fridge Storage",
     "photo": "vegetables",
     "image": "https://cdn.stocksnap.io/img-thumbs/960w/RYSFFCA1QV.jpg",
     "credit": "",
     "title": "Keep onions and potatoes apart in the pantry",
     "body": "Onions give off moisture and gas that makes potatoes sprout "
             "faster. Two separate ventilated containers, somewhere dark, "
             "keeps both for weeks longer."},
    {"board": "Pantry and Fridge Storage",
     "photo": "eggs in a carton",
     "image": "https://cdn.stocksnap.io/img-thumbs/960w/MGWWJDK49D.jpg",
     "credit": "",
     "title": "Keep eggs in their carton, pointed end down",
     "body": "The carton protects against odours and knocks, and the pointed "
             "end down keeps the yolk centred. Fridge-door egg racks are the "
             "worst of the available options."},
    {"board": "Pantry and Fridge Storage",
     "photo": "bread",
     "image": "https://cdn.stocksnap.io/img-thumbs/960w/9J9OUZYDZ3.jpg",
     "credit": "",
     "title": "Bread goes stale faster in the fridge, not slower",
     "body": "Refrigeration speeds up the staling process rather than "
             "slowing it. A cool cupboard for a few days, or the freezer for "
             "anything longer, keeps the loaf far better."},
    {"board": "Pantry and Fridge Storage",
     "photo": "cereal",
     "image": "https://cdn.stocksnap.io/img-thumbs/960w/BC5DD62102.jpg",
     "credit": "",
     "title": "Group pantry food by meal, not by packet size",
     "body": "Breakfast things together, baking things together. Organising "
             "a pantry by what you are about to cook cuts the searching more "
             "than any container system does."},
    {"board": "Pantry and Fridge Storage",
     "photo": "kitchen jars",
     "image": "https://cdn.stocksnap.io/img-thumbs/960w/9G5X445AP9.jpg",
     "credit": "",
     "title": "Leave one shelf empty and the pantry stays tidy",
     "body": "A pantry filled to capacity has nowhere to put a shop, so "
             "things end up on the worktop. One deliberately empty shelf is "
             "what keeps the rest of it tidy."},
    # ── Under Sink and Cabinet Storage ──────────────────────────────
    {"board": "Under Sink and Cabinet Storage",
     "photo": "kitchen sink",
     "image": "https://cdn.stocksnap.io/img-thumbs/960w/L07UXRLREE.jpg",
     "credit": "",
     "title": "Work around the pipes under the sink, not against them",
     "body": "The waste pipe makes a single large container impossible. Two "
             "narrow ones either side use the same space and still let you "
             "reach the stopcock."},
    {"board": "Under Sink and Cabinet Storage",
     "photo": "kitchen cabinet",
     "image": "https://cdn.stocksnap.io/img-thumbs/960w/CTKGR7O9UB.jpg",
     "credit": "",
     "title": "Anything on wheels under a cabinet gets used",
     "body": "Storage you have to kneel down and reach into stays empty. If "
             "it slides out to you, you will use the back of the cupboard "
             "for the first time."},
    {"board": "Under Sink and Cabinet Storage",
     "photo": "spray bottle cleaning",
     "image": "https://images.rawpixel.com/editor_1024/czNmcy1wcml2YXRlL3Jhd3BpeGVsX2ltYWdlcy93ZWJzaXRlX2NvbnRlbnQvbHIvcHg5MjMzNDItaW1hZ2Uta3d2dXRhMncuanBn.jpg",
     "credit": "",
     "title": "Hang spray bottles by the trigger under the sink",
     "body": "A tension rod across the cupboard lets bottles hang by their "
             "trigger heads, which frees the whole floor of the cabinet for "
             "something else."},
    {"board": "Under Sink and Cabinet Storage",
     "photo": "open shelving kitchen",
     "image": "https://images.rawpixel.com/editor_1024/czNmcy1wcml2YXRlL3Jhd3BpeGVsX2ltYWdlcy93ZWJzaXRlX2NvbnRlbnQvbHIvcHgxMTE5MjU0LWltYWdlLWt3dnk0YzVrLmpwZw.jpg",
     "credit": "",
     "title": "A riser turns one cabinet shelf into two",
     "body": "Most cupboards have twenty wasted centimetres above the "
             "plates. A shelf riser is the one change that adds cabinet "
             "capacity without moving anything."},
    {"board": "Under Sink and Cabinet Storage",
     "photo": "cutlery drawer",
     "image": "https://images.rawpixel.com/editor_1024/czNmcy1wcml2YXRlL3Jhd3BpeGVsX2ltYWdlcy93ZWJzaXRlX2NvbnRlbnQvbHIvdXB3azYyMTE5OTY4LXdpa2ltZWRpYS1pbWFnZS1rb3dnb3pyMy5qcGc.jpg",
     "credit": "",
     "title": "Stack pans with the lids stored separately",
     "body": "Nested pans with lids on take almost twice the height. Lids in "
             "a rack or a drawer divider, pans stacked bare, and a cupboard "
             "holds half as much again."},
    {"board": "Under Sink and Cabinet Storage",
     "photo": "kitchen sponge",
     "image": "https://images.rawpixel.com/editor_1024/czNmcy1wcml2YXRlL3Jhd3BpeGVsX2ltYWdlcy93ZWJzaXRlX2NvbnRlbnQvbHIvcHg5ODc4OTctaW1hZ2Uta3d2eGF2NGMuanBn.jpg",
     "credit": "",
     "title": "Give the kitchen sponge somewhere to drain",
     "body": "A sponge left flat in the sink stays wet and starts to smell "
             "within a day. Anything that lets air underneath it -- a caddy, "
             "a clip, a suction holder -- solves it."},
    {"board": "Under Sink and Cabinet Storage",
     "photo": "cleaning bucket",
     "image": "https://images.rawpixel.com/editor_1024/cHJpdmF0ZS9zdGF0aWMvaW1hZ2Uvd2Vic2l0ZS8yMDIyLTA0L2xyL3B4MTA4NjczNi1pbWFnZS1rd3Z3YnNxYi5qcGc.jpg",
     "credit": "",
     "title": "Keep a bucket you can carry, not one you fill",
     "body": "The cleaning things you use are the ones you can pick up in "
             "one trip. A small caddy that goes room to room beats a large "
             "cupboard you have to visit repeatedly."},
    # ── Tiny Apartment Solutions ────────────────────────────────────
    {"board": "Tiny Apartment Solutions",
     "photo": "closet",
     "image": "https://cdn.stocksnap.io/img-thumbs/960w/D1B0FAEB46.jpg",
     "credit": "",
     "title": "Hang what creases and fold the rest to save space",
     "body": "Closet space runs out because everything gets hung. Knitwear "
             "and jeans are happier folded, and hanging only what needs it "
             "roughly halves the rail you need."},
    {"board": "Tiny Apartment Solutions",
     "photo": "studio apartment interior",
     "image": "https://cdn.stocksnap.io/img-thumbs/960w/YED8JWCWVB.jpg",
     "credit": "",
     "title": "In a small flat, storage goes up the wall",
     "body": "Floor space is the thing you are short of; wall height is the "
             "thing you have. Shelves above door height hold everything used "
             "seasonally."},
    {"board": "Tiny Apartment Solutions",
     "photo": "shoes by the door",
     "image": "https://images.rawpixel.com/editor_1024/cHJpdmF0ZS9sci9pbWFnZXMvd2Vic2l0ZS8yMDIyLTA0L3Vwd2s2MTg0NzM1Ni13aWtpbWVkaWEtaW1hZ2Uta293cGsyYTEuanBn.jpg",
     "credit": "",
     "title": "Keep only this week's shoes by the front door",
     "body": "An entryway holds four pairs before it looks chaotic. "
             "Everything else belongs in a wardrobe, and the hallway stays "
             "walkable."},
    {"board": "Tiny Apartment Solutions",
     "photo": "wardrobe",
     "image": "https://cdn.stocksnap.io/img-thumbs/960w/YISUDLOYC3.jpg",
     "credit": "",
     "title": "Store out-of-season clothes at the top of the closet",
     "body": "Rotating winter and summer twice a year keeps the reachable "
             "half of a wardrobe holding only things you might wear this "
             "week."},
    {"board": "Tiny Apartment Solutions",
     "photo": "coat hooks wall",
     "image": "https://images.rawpixel.com/editor_1024/czNmcy1wcml2YXRlL3Jhd3BpeGVsX2ltYWdlcy93ZWJzaXRlX2NvbnRlbnQvbHIvbG9jMjAxNzc4NTg2OS1pbWFnZS5qcGc.jpg",
     "credit": "",
     "title": "Coat hooks beat hangers in a narrow hallway",
     "body": "A coat on a hook takes seconds and the depth of the coat. A "
             "hanger needs a rail, a cupboard and the swing of a door."},
    {"board": "Tiny Apartment Solutions",
     "photo": "storage basket",
     "image": "https://images.rawpixel.com/editor_1024/czNmcy1wcml2YXRlL3Jhd3BpeGVsX2ltYWdlcy93ZWJzaXRlX2NvbnRlbnQvbHIvZnJiYXNrZXRfZnJ1aXRzX3doaXRlX25hdHVyZS1pbWFnZS1reWJlYmxtZS5qcGc.jpg",
     "credit": "",
     "title": "Open baskets for things you use, lids for things you keep",
     "body": "A lid is a small obstacle that stops daily items being put "
             "away. Save lids for what is genuinely being stored rather than "
             "used."},
    {"board": "Tiny Apartment Solutions",
     "photo": "bed",
     "image": "https://pd.w.org/2026/05/1276a00258120ad17.48098578-2048x1536.jpeg",
     "credit": "",
     "title": "Under the bed is the largest cupboard you own",
     "body": "It is also the one that collects dust. Flat, closed containers "
             "on the floor beneath a bed hold bedding and off-season clothes "
             "and keep them clean."},
    {"board": "Tiny Apartment Solutions",
     "photo": "small kitchen interior",
     "image": "https://images.rawpixel.com/editor_1024/czNmcy1wcml2YXRlL3Jhd3BpeGVsX2ltYWdlcy93ZWJzaXRlX2NvbnRlbnQvbHIvdXB3azYxNzYxOTAyLXdpa2ltZWRpYS1pbWFnZS1rb3dsY3Z4ay5qcGc.jpg",
     "credit": "",
     "title": "In a tiny kitchen, clear one worktop completely",
     "body": "One genuinely empty surface makes a small kitchen usable. It "
             "is worth more than any storage you could add, and it costs "
             "nothing but a decision about where things live."},
    {"board": "Tiny Apartment Solutions",
     "photo": "hallway home interior",
     "image": "https://images.rawpixel.com/editor_1024/czNmcy1wcml2YXRlL3Jhd3BpeGVsX2ltYWdlcy93ZWJzaXRlX2NvbnRlbnQvbHIvcHUyMzMzOTY2LWltYWdlLWt3eXJvM2pjLmpwZw.jpg",
     "credit": "",
     "title": "Give keys a home within arm's reach of the door",
     "body": "A hook or a small dish inside the door removes the most "
             "repeated search in most homes. Storage works when it is where "
             "the habit already is."},
    # ── Kitchen Gadgets Worth Buying ────────────────────────────────
    {"board": "Kitchen Gadgets Worth Buying",
     "photo": "kitchen scale",
     "image": "https://images.rawpixel.com/editor_1024/czNmcy1wcml2YXRlL3Jhd3BpeGVsX2ltYWdlcy93ZWJzaXRlX2NvbnRlbnQvbHIvZnJob3Jpem9udGFsX2tpdGNoZW5fc2NhbGVfYmxhY2staW1hZ2Uta3liZGgxOTAuanBn.jpg",
     "credit": "",
     "title": "A scale makes baking work the first time",
     "body": "Cup measures vary with how you fill them; grams do not. It is "
             "the difference between a recipe that works and one that works "
             "sometimes."},
    {"board": "Kitchen Gadgets Worth Buying",
     "photo": "chopping board vegetables",
     "image": "https://cdn.stocksnap.io/img-thumbs/960w/03JNS3TYGF.jpg",
     "credit": "",
     "title": "Keep one board for raw meat and never mix them",
     "body": "Colour or shape, it does not matter -- what matters is that "
             "the distinction is obvious enough that a tired person at the "
             "end of the day still gets it right."},
    {"board": "Kitchen Gadgets Worth Buying",
     "photo": "kitchen knife",
     "image": "https://images.rawpixel.com/editor_1024/cHJpdmF0ZS9zdGF0aWMvaW1hZ2Uvd2Vic2l0ZS8yMDIyLTA0L2xyL3B4MTMyNTMzNC1pbWFnZS1rd3Z3NDEzdy5qcGc.jpg",
     "credit": "",
     "title": "A sharp kitchen knife is safer than a blunt one",
     "body": "Blunt blades slip because you push harder. A honing steel used "
             "for ten seconds before cooking does more for safety than any "
             "cut-resistant glove."},
    {"board": "Kitchen Gadgets Worth Buying",
     "photo": "peeling a potato",
     "image": "https://images.rawpixel.com/editor_1024/cHJpdmF0ZS9zdGF0aWMvaW1hZ2Uvd2Vic2l0ZS8yMDIyLTA0L2xyL3B1MjMzMzM2NS1pbWFnZS1rd3Z3bTF2dS5qcGc.jpg",
     "credit": "",
     "title": "Peel vegetables towards a bowl, not the board",
     "body": "Peelings go straight where they belong and the worktop stays "
             "clean. It is a habit rather than a purchase, and it saves a "
             "wipe-down every time."},
    {"board": "Kitchen Gadgets Worth Buying",
     "photo": "measuring spoons",
     "image": "https://pd.w.org/2022/10/23363483420b7dba1.77035555-2048x1365.jpg",
     "credit": "",
     "title": "Keep measuring spoons loose, not on a ring",
     "body": "A ring means washing all of them to use one. Separated in a "
             "drawer, you wash what you used."},
    {"board": "Kitchen Gadgets Worth Buying",
     "photo": "colander",
     "image": "https://images.rawpixel.com/editor_1024/czNmcy1wcml2YXRlL3Jhd3BpeGVsX2ltYWdlcy93ZWJzaXRlX2NvbnRlbnQvbHIvdXB3azYxNjY2NjUyLXdpa2ltZWRpYS1pbWFnZS1rb3diMGozcS5qcGc.jpg",
     "credit": "",
     "title": "A collapsible colander earns its cupboard space",
     "body": "Rigid colanders are mostly air and take a whole shelf. One "
             "that folds flat does the same job and stores in a drawer."},
    {"board": "Kitchen Gadgets Worth Buying",
     "photo": "jar lid",
     "image": "https://images.rawpixel.com/editor_1024/cHJpdmF0ZS9sci9pbWFnZXMvd2Vic2l0ZS8yMDIzLTAzL2NsZTE5OTctLTIwLS1iLWltYWdlLmpwZw.jpg",
     "credit": "",
     "title": "Hot water opens a stuck jar lid in seconds",
     "body": "Thirty seconds of hot tap water on the lid expands the metal "
             "just enough. Gadgets help, but this costs nothing and works "
             "most of the time."},
    {"board": "Kitchen Gadgets Worth Buying",
     "photo": "kitchen timer",
     "image": "https://images.rawpixel.com/editor_1024/czNmcy1wcml2YXRlL3Jhd3BpeGVsX2ltYWdlcy93ZWJzaXRlX2NvbnRlbnQvbHIvZnJlZ2dfYWxhcm1fY2xvY2tfZ3JleS1pbWFnZS1reWJkdTIxdC5qcGc.jpg",
     "credit": "",
     "title": "Set the timer before you start, not after",
     "body": "The minutes you lose are the ones between putting food on and "
             "remembering to time it. Setting it first is the whole "
             "technique."},
    # ── Small Kitchen Organization ──────────────────────────────────
    {"board": "Small Kitchen Organization",
     "photo": "kitchen utensils",
     "image": "https://cdn.stocksnap.io/img-thumbs/960w/F908L9RJSI.jpg",
     "credit": "",
     "title": "Keep kitchen utensils where you stand to cook",
     "body": "A pot by the hob holds the five things you reach for while "
             "cooking. Everything else can live in a drawer somewhere less "
             "convenient."},
    {"board": "Small Kitchen Organization",
     "photo": "kitchen countertop",
     "image": "https://pd.w.org/2026/04/43169ebd8014631b8.76406805-2048x1536.jpg",
     "credit": "",
     "title": "Only daily appliances deserve worktop space",
     "body": "A kettle earns its place; a blender used monthly does not. "
             "Worktop is the most expensive storage in the kitchen and "
             "should be charged accordingly."},
    {"board": "Small Kitchen Organization",
     "photo": "kitchen counter",
     "image": "https://images.rawpixel.com/editor_1024/czNmcy1wcml2YXRlL3Jhd3BpeGVsX2ltYWdlcy93ZWJzaXRlX2NvbnRlbnQvbHIvZmw1MTQ4NjUzOTM1Mi1pbWFnZS1reWNpbXJucC5qcGc.jpg",
     "credit": "",
     "title": "Put things away where you first use them",
     "body": "Tea near the kettle, knives near the board, pans near the hob. "
             "Organising by habit rather than by category is what makes a "
             "tidy kitchen stay tidy."},
    {"board": "Small Kitchen Organization",
     "photo": "kitchen towel",
     "image": "https://images.rawpixel.com/editor_1024/cHJpdmF0ZS9zdGF0aWMvaW1hZ2Uvd2Vic2l0ZS8yMDIyLTA0L2xyL3B4NjE0ODA2LWltYWdlLWt3dnZidDR0LmpwZw.jpg",
     "credit": "",
     "title": "Keep two tea towels: one for hands, one for dishes",
     "body": "A single towel is always damp and doing neither job well. Two "
             "hooks is a smaller change than it sounds and a noticeable "
             "improvement."},
]


# Recorded in the pin_posts `angle` column, which is what tells an advice pin
# from a product pin everywhere else -- including for a pin reloaded from the
# database, where the in-memory `kind` does not survive.
VALUE_ANGLE = "tip"

# Hashtags per board. NONE of these may contain "#ad", "#affiliate" or
# "#sponsored" as a substring: an advice pin sells nothing, so a disclosure
# marker on one would be a false statement about what it is, and the
# compliance gate refuses the pin outright if it finds one.
BOARD_TAGS: Dict[str, tuple] = {
    "Bathroom Storage Ideas": (
        "#bathroomstorage", "#bathroomorganization", "#smallbathroom"),
    "Pantry and Fridge Storage": (
        "#pantryorganization", "#fridgeorganization", "#kitchenstorage"),
    "Under Sink and Cabinet Storage": (
        "#kitchenstorage", "#undersinkorganization", "#cabinetorganization"),
    "Tiny Apartment Solutions": (
        "#smallspaceliving", "#apartmentliving", "#spacesaving"),
    "Kitchen Gadgets Worth Buying": (
        "#kitchentips", "#cookingtips", "#kitchenhacks"),
    "Small Kitchen Organization": (
        "#smallkitchen", "#kitchenorganization", "#homeorganizing"),
}

GENERAL_TAGS = ("#homeorganization", "#organizingtips")


def eyebrow_for(board: str) -> str:
    """
    The small label printed above the title on the pin.

    Taken from the board so the pin says what part of the home it is about.
    The board's own sales language is dropped -- "Worth Buying" on a pin that
    sells nothing would be the one dishonest word on it.
    """
    label = (board or "").replace(" Worth Buying", "").replace(" Ideas", "")
    return label.strip() or "Around the house"


def describe(tip: Dict[str, str], board: str, credit: str = "") -> str:
    """
    The pin description: the advice, then hashtags, then any photo credit.

    NO DISCLOSURE, because there is nothing to disclose -- no link, no
    commission, nothing sold. Adding "#ad" here to be safe would be the
    opposite of safe: it would tell Pinterest this pin is promotional too,
    and the whole point of it is that four pins in five are not.

    `credit` comes back from the photo finder, and is empty for public
    domain pictures. When it is not empty the licence requires attribution,
    so it goes in the description exactly as the website does it.
    """
    tags = list(BOARD_TAGS.get(board, ())) + list(GENERAL_TAGS)
    parts = [tip["body"].strip(), " ".join(tags)]
    if credit:
        parts.append(credit.strip())
    return "\n\n".join(p for p in parts if p)


def _key(title: str) -> str:
    return " ".join(sorted((title or "").lower().split()))


def next_tip(recent_titles: Optional[List[str]] = None) -> Optional[Dict[str, str]]:
    """
    A tip that has not been used lately.

    Shuffled rather than cycled in order: a fixed order makes the board read
    top-to-bottom as a list, which is the look the whole exercise is trying
    to avoid.
    """
    if not TIP_BANK:
        return None

    seen = {_key(t) for t in (recent_titles or [])}
    fresh = [t for t in TIP_BANK if _key(t["title"]) not in seen]
    if not fresh:
        # Every tip has run recently. Better to repeat the oldest than to
        # publish nothing -- an empty slot helps nobody.
        logger.info("Tip bank exhausted against recent history; reusing.")
        fresh = list(TIP_BANK)
    return random.choice(fresh)
