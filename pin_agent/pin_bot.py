"""
The pin agent orchestrator.

Runs the six stages in order: source, select, write, build the image, check
compliance, publish. Nothing reaches Pinterest without passing the compliance
gate, and while review is on nothing publishes without a human approving it.

Off by default. Novi's dashboard owns the switch, so this only runs when
`brain.pin_module_active` is true.
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from pin_agent import boards as board_routing
from pin_agent import product_types
from pin_agent import tips as tip_bank
from pin_agent.tip_writer import TipWriter
from pin_agent.compliance import ComplianceGate
from pin_agent.photo_check import PhotoVerifier
from pin_agent.content import PinCopywriter
from pin_agent.imaging import PinImageBuilder
from pin_agent.publisher import PinterestPublisher
from pin_agent.selector import ProductSelector
from pin_agent.sourcing import AliExpressClient
from pin_agent.store import PinStore

logger = logging.getLogger("PinAgent")


class PinAgent:
    """AliExpress to Pinterest, end to end."""

    def __init__(self, config, ai_engine, supabase_client=None,
                 notification_manager=None, image_dir: str = "assets/pins",
                 upload_image=None, photos=None):
        self.config = config
        self.nm = notification_manager
        # Uploads a local file and returns a public URL. Buffer fetches the
        # image itself, so an un-hosted pin cannot be published at all.
        self.upload_image = upload_image

        self.sourcing = AliExpressClient(
            app_key=config.ali_app_key, app_secret=config.ali_app_secret,
            tracking_id=config.ali_tracking_id, niche=config.niche)
        self.selector = ProductSelector(
            min_rating=config.min_rating, min_orders=config.min_orders,
            min_price=config.min_price, max_price=config.max_price)
        self.copywriter = PinCopywriter(ai_engine, brand=config.pin_brand,
                                        niche=config.niche)
        self.imaging = PinImageBuilder(image_dir, brand=config.pin_brand)
        self.gate = ComplianceGate()
        # Writes the advice pins. Text only -- it never picks the picture
        # and it never decides whether a pin may publish.
        self.writer = TipWriter(ai_engine) if ai_engine else None
        # Finds candidate photographs for a freshly written tip.
        self.photos = photos
        # Looks at the photograph and says whether it shows the thing. The
        # one check in the pipeline that reads the image rather than text
        # about it.
        self.verifier = PhotoVerifier()
        self.publisher = PinterestPublisher(
            access_token=config.buffer_token,
            organization_id=config.buffer_organization_id,
            board_id=config.buffer_board_id,
            db=None, max_queued=config.max_queued,
            channel_id=config.buffer_channel_id)
        self.store = PinStore(supabase_client)

        # Pins waiting for a human yes/no while review is on.
        self.pending_review: List[Dict] = []
        self.published_today = 0
        # When the very first pin went out. Read from the pins themselves
        # on connect, so a redeploy cannot reset the ramp.
        self._first_pin_at: Optional[datetime] = None
        # Titles of recent PRODUCT pins, so the same product from a different
        # seller is not pinned twice days apart.
        self.recent_titles: List[str] = []
        # Titles of recent ADVICE pins, kept apart so the tip bank rotates
        # without the product duplicate guard ever seeing them.
        self.recent_tips: List[str] = []
        # Product types pinned inside the cooldown window. Nine of the first
        # fifty-five pins were spice racks; this is what stops that.
        self.recent_types: List[str] = []
        # Boards the advice pins went to lately, so the writer feeds the
        # quietest one next rather than the same one repeatedly.
        self.recent_boards: List[str] = []
        # Consecutive slots that produced nothing. See _note_failure.
        self._consecutive_failures = 0
        self.last_run: Optional[datetime] = None
        self.last_error = ""

    # ── status ───────────────────────────────────────────────────

    @property
    def status(self) -> Dict:
        return {
            "niche": self.config.niche,
            "aliexpress_live": self.sourcing.is_live,
            "publisher": self.publisher.status,
            "published_today": self.published_today,
            "max_per_day": self.daily_cap(),
            "max_per_day_configured": self.config.pins_per_day,
            # How many of today's pins may carry an affiliate link. The rest
            # are advice pins with no destination at all.
            "affiliate_pins_per_day": self.product_quota(),
            "tips_written": (self.writer.status if self.writer else {}),
            "photo_checks": self.verifier.status,
            "tips_in_bank": len(tip_bank.TIP_BANK),
            "subject_cooldown_days": product_types.COOLDOWN_DAYS,
            "subjects_on_cooldown": len(set(self.recent_types)),
            "days_live": self.days_live,
            "awaiting_review": len(self.pending_review),
            "review_required": self.config.require_review,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "consecutive_failures": self._consecutive_failures,
            "last_error": self.last_error,
        }

    # WHEN PINS GO OUT, and why none of it is keyed to Pakistan.
    #
    # Pinterest's audience is overwhelmingly American, and the site behaves
    # like a search engine with an evening browsing peak rather than a feed.
    # The hours that matter are US afternoon and evening: 8-11pm Eastern
    # first, then early afternoon.
    #
    # PKT is Eastern + 9, so those peaks land between 23:00 and 08:00 local
    # time. The bot's sleep window was 23:00-07:00 -- it covered almost
    # exactly the best hours Pinterest has. Pins now run above that gate:
    # the window exists so a TELEGRAM account looks like a person who sleeps,
    # and a pin has no such problem.
    #
    # Ordered best-first. Today's cap takes the top N, so a day at four pins
    # uses the four strongest slots rather than the four earliest.
    #        PKT     US Eastern
    SLOT_PRIORITY = [
        (5, 0),    # 20:00  peak evening browsing
        (23, 0),   # 14:00  early afternoon
        (6, 0),    # 21:00
        (1, 0),    # 16:00
        (7, 0),    # 22:00
        (0, 0),    # 15:00
        (21, 0),   # 12:00  lunch
        (8, 0),    # 23:00
        (2, 0),    # 17:00
        (20, 0),   # 11:00
        (18, 0),   # 09:00
        (4, 0),    # 19:00
        (22, 0),   # 13:00
        (3, 0),    # 18:00
        (19, 0),   # 10:00
    ]

    # How long a slot stays open. The main loop ticks every 60 seconds, so
    # this is generous on purpose: a slow sourcing call or a restart must not
    # cause the slot to be missed entirely.
    SLOT_WINDOW_MINUTES = 25

    @staticmethod
    def _pkt_now() -> datetime:
        return datetime.now(timezone.utc) + timedelta(hours=5)

    def due_slot(self) -> Optional[Dict]:
        """
        The slot that is open right now, or None.

        Only slots inside today's cap count, so the ramp decides how many of
        the fifteen are live rather than the agent simply stopping once it
        hits a number.
        """
        now = self._pkt_now()
        for rank, (hour, minute) in enumerate(self.SLOT_PRIORITY):
            if rank >= self.daily_cap():
                break
            if (now.hour == hour
                    and minute <= now.minute < minute + self.SLOT_WINDOW_MINUTES):
                return {"hour": hour, "minute": minute,
                        "rank": rank + 1, "key": f"pin_{hour}_{minute}"}
        return None

    async def slot_already_filled(self) -> bool:
        """
        Whether a pin already went out inside the current window.

        Asked of the DATABASE, not of memory: a restart inside the window
        would otherwise make the slot look unfired and publish a second pin.
        That is exactly how two articles went out at 01:01 and 01:22.
        """
        try:
            return await self.store.published_since(self.SLOT_WINDOW_MINUTES) > 0
        except Exception as e:
            # A failed check must not block publishing; the daily cap is
            # still counted separately.
            logger.warning(f"Slot check failed ({type(e).__name__}); "
                           f"continuing.")
            return False

    # A brand-new Pinterest account that starts at fifteen pins a day looks
    # exactly like a bought account being drained, and the reach penalty for
    # that is not something you appeal. Volume is earned instead: each step
    # holds for ten days, which is long enough for Pinterest to see the
    # account behave consistently at that level.
    #
    #   (day the step ends, pins allowed up to that day)
    #
    # LOWERED AFTER READING WHAT PINTEREST ACTUALLY REWARDS. The ramp used to
    # climb to eight by day thirty and fifteen after that. Every current
    # source puts the safe range for a young account at one to five fresh
    # pins a day, ten at the outside, and says the same thing about pace:
    # "1-5 fresh pins per day, every day, outperforms 30 pins in one burst
    # followed by silence". This account is two weeks old with three
    # followers and no saves; the constraint is not how much it posts.
    #
    # Five also divides cleanly into the affiliate ratio: one pin that sells,
    # four that do not.
    RAMP = ((10, 4), (20, 5), (30, 5), (45, 6))
    RAMP_CEILING = 8

    def daily_cap(self) -> int:
        """
        Today's ceiling, which grows with the account's age.

        Never above PIN_MAX_PER_DAY: the ramp raises the floor over time but
        the configured number is still the limit the owner asked for.
        """
        configured = self.config.pins_per_day
        # No pin has gone out yet, so this is day zero -- the newest the
        # account will ever be. Falling back to the configured maximum here
        # would let the very first day run at full volume, which is exactly
        # the day the ramp exists to protect.
        age = self.days_live if self.days_live is not None else 0

        for last_day, allowed in self.RAMP:
            if age <= last_day:
                return min(configured, allowed)
        return min(configured, self.RAMP_CEILING)

    @property
    def days_live(self) -> Optional[int]:
        """
        Days since the first pin went out, or None before there is one.

        Derived from the pins themselves rather than stored separately, so a
        redeploy cannot reset it -- the same mistake that silently raised the
        social module's cap back to full on day one.
        """
        if not self._first_pin_at:
            return None
        delta = datetime.now(timezone.utc) - self._first_pin_at
        return max(0, delta.days)

    # Two pin titles sharing this much of their meaningful vocabulary are
    # describing the same product. "4-layer adjustable spice drawer
    # organizer" and "4-layer adjustable spice rack slides into a drawer"
    # share five words out of seven.
    TITLE_OVERLAP = 0.5

    # Meaningful words in common that mean "same product" regardless of
    # ratio. Two genuinely different listings on this board shared NONE.
    SHARED_WORDS = 3

    # Titles are compared against roughly three weeks of pins.
    #
    # FORTY WAS TOO FEW once volume rose. At six pins a day it held 6.7 days,
    # so "Clear Your Counter with a Pull-Out Spice Drawer" (2 Sep) had
    # dropped out of memory before "Keep your kitchen clear with a spice
    # drawer organizer" was written on the 12th, and again on the 13th. The
    # guard was working; it simply could not see back far enough.
    RECENT_TITLE_COUNT = 120

    _TITLE_NOISE = {
        "this", "that", "the", "a", "an", "and", "or", "with", "for", "your",
        "you", "keep", "make", "makes", "made", "from", "into", "onto", "any",
        "these", "those", "our", "ours", "their", "them", "who", "what",
        "every", "all", "one", "two", "get", "gets", "have", "has", "its",
        "it", "in", "on", "of", "to", "is", "are", "up", "out", "off", "at",
        "by", "no", "so", "can", "will", "perfect", "great", "ideal", "best",
        "kitchen", "home", "space", "saving", "saver", "small", "clear",
        "tidy", "organized", "organised", "organizer", "storage",

        # ADDED AFTER A THIRTY-TWO HOUR OUTAGE.
        #
        # Every pin in this niche is a compact space-saving shelf or rack,
        # so none of these words tells one product from another. Left in,
        # the guard matched "Compact corner shelf with hooks" against
        # "Compact bathroom shelf saves space" -- two different products
        # sharing nothing but marketing vocabulary. Six candidates in a row
        # were rejected, build_one() returned None, and the agent quietly
        # published nothing for a day and a half while the website carried
        # on normally.
        #
        # The words that actually IDENTIFY a product stay: spice, cutlery,
        # egg, shoe, towel, toothbrush, sink, fridge, pantry, jar, bottle.
        "compact", "shelf", "shelves", "rack", "holder", "bin", "bins",
        "box", "boxes", "tray", "set", "wall", "door", "hanging", "sliding",
        "tier", "expandable", "stackable", "mount", "mounted", "saves",
        "save", "maximize", "maximise", "fits", "fit", "easy", "simple",
        "sleek", "elegant", "versatile", "handy", "neat", "neatly", "more",
        "less", "within", "reach", "room", "counter", "countertop",
        "drawer", "drawers", "cabinet", "cupboard", "solution", "helps",
        "help", "keeps", "tiny", "white", "black", "clutter", "declutter",
        "minutes", "daily", "gift", "idea", "ideas", "must", "quick",
    }

    @classmethod
    def _title_words(cls, title: str) -> set:
        words = re.findall(r"[a-z0-9]+", (title or "").lower())
        return {w for w in words if len(w) > 2 and w not in cls._TITLE_NOISE}

    def _too_similar_to_recent(self, title: str) -> bool:
        """
        Whether this pin describes something already pinned lately.

        Compares meaningful words only. The generic vocabulary of the niche
        -- kitchen, storage, organizer, space-saving -- appears in every
        title and would make everything look like a duplicate, so it is
        stripped before comparing.
        """
        words = self._title_words(title)
        if len(words) < 2:
            return False

        for previous in self.recent_titles:
            other = self._title_words(previous)
            if len(other) < 2:
                continue
            common = words & other
            # A ratio alone is not enough, in BOTH directions.
            #
            # Too loose: it collapses when a title reduces to one meaningful
            # word -- "kitchen storage organizer for small space saving
            # homes" leaves just {homes}, and one shared word out of one is
            # a perfect score.
            #
            # Too tight: comparing full pin text, the word sets get large
            # and a real duplicate scores badly. Two listings of the same
            # herb scissors shared {scissors, stainless, steel} but only
            # rated 0.40, and the second one published.
            #
            # So an absolute count catches what the ratio misses. Measured
            # against the live board: three shared words caught all four
            # confirmed duplicates and flagged none of four genuinely
            # different products, which shared no meaningful words at all.
            if len(common) >= self.SHARED_WORDS:
                return True
            if (len(common) >= 2
                    and len(common) / min(len(words), len(other))
                    >= self.TITLE_OVERLAP):
                return True
        return False

    async def connect(self) -> bool:
        ok = await self.publisher.connect()
        history = await self.store.posted_history()
        self.gate.load_history(history["product_ids"], history["urls"],
                               history["image_hashes"])
        self.published_today = await self.store.posted_today()
        self._first_pin_at = await self.store.first_pin_at()
        self.recent_titles = await self.store.recent_titles(
            self.RECENT_TITLE_COUNT, kind="product")
        self.recent_tips = await self.store.recent_tip_titles(
            self.TIP_ROTATION)
        self.recent_types = await self.store.recent_types(
            product_types.COOLDOWN_DAYS)

        # Pins that were waiting for a decision when the process last
        # stopped. Without this the queue is empty after every deploy and
        # Approve has nothing to act on, while the products stay burned in
        # the dedupe set for four months.
        self.pending_review = await self.store.pending_pins()
        if self.pending_review:
            logger.info(f"Restored {len(self.pending_review)} pin(s) still "
                        f"awaiting review.")
        # Bias future picks toward what has actually performed.
        self.selector.performance = await self.store.category_performance()
        return ok

    # ── the pipeline ─────────────────────────────────────────────

    async def build_one(self) -> Optional[Dict]:
        """
        Produces one publishable pin, or None.

        Stops at the first product that survives every stage rather than
        preparing a batch, because each stage costs an API call or an image
        download and most rejections happen early.
        """
        products = await self.sourcing.fetch_products()
        if not products:
            self.last_error = self.sourcing.last_error or "no products returned"
            logger.warning(f"Sourcing produced nothing: {self.last_error}")
            return None

        candidates = self.selector.select(
            products, limit=6, exclude_ids=self.gate.seen_products)
        if not candidates:
            self.last_error = "no product passed the filters"
            logger.info(self.last_error)
            return None

        recent_angles = await self.store.recent_angles()

        for product in candidates:
            copy = await self.copywriter.write(product, recent_angles)
            if not copy:
                continue

            # THE SAME ITEM FROM A DIFFERENT SELLER, DAYS APART.
            #
            # Deduplicating on product_id only catches the identical
            # listing. Two sellers list the same 4-layer spice drawer
            # organiser under different ids, and both were pinned a day
            # apart -- which on a young board is the most visible possible
            # sign of automation.
            #
            # Checked here, after the copy and before the image, because
            # the copy is what describes the product and the image is the
            # expensive step.
            # TITLE against TITLE. Nothing else.
            #
            # This compared the candidate's title PLUS its description and
            # hashtags -- about twenty-five words -- against history entries
            # that hold the title alone, about five. Sharing three generic
            # words is then close to certain, and the guard rejected six
            # candidates in a row: "kitchens", "spaces", "design",
            # "homeorganization", "smallkitchen". The agent published
            # nothing for thirty-two hours while the website carried on.
            #
            # Both sides must be the same kind of text or the counts mean
            # nothing.
            if self._too_similar_to_recent(copy["title"]):
                logger.info(f"Skipping '{copy['title'][:44]}' — too close to "
                            f"something pinned recently.")
                continue

            # THE SAME KIND OF THING, WEEK AFTER WEEK.
            #
            # Nine of the first fifty-five pins were spice racks, seven of
            # them inside twelve days. Every one was a different listing with
            # a different link and a different photograph, so the id, url and
            # image checks all passed, and the title guard could not see it
            # either -- "drawer", "organizer" and "clear" are on the noise
            # list, so two spice pins shared one meaningful word against a
            # bar of three.
            #
            # Asked on the TITLE alone. Descriptions mention other things in
            # passing, and classifying on them put a rolling cart and a pair
            # of scissors in the spice bucket.
            product_type = product_types.classify(copy["title"])
            if product_types.blocked_by_cooldown(product_type,
                                                 self.recent_types):
                logger.info(
                    f"Skipping '{copy['title'][:40]}' — another "
                    f"{product_types.describe(product_type)} was pinned in "
                    f"the last {product_types.COOLDOWN_DAYS} days.")
                continue

            image_path, image_bytes = await self.imaging.build(
                product, copy["title"],
                eyebrow=copy["angle"].replace("_", " "))
            if not image_path:
                logger.warning("Pin image could not be written; skipping product.")
                continue

            image_hash = self.gate.image_fingerprint(image_bytes)

            # Buffer downloads the image, so it has to be hosted before the
            # compliance check can pass.
            image_url = ""
            if self.upload_image:
                image_url = await self.upload_image(image_path) or ""

            pin = {
                "product_id": product["product_id"],
                "title": copy["title"],
                "description": copy["description"],
                "link": product["affiliate_url"],
                "image_path": image_path,
                "image_url": image_url,
                "image_hash": image_hash,
                "angle": copy["angle"],
                # The PRODUCT TYPE, not the AliExpress category. That column
                # held the string "Home & Garden" on all 47 pins -- one value
                # for everything, so it carried no information and nothing
                # read it. The type is what the cooldown counts, and it also
                # gives category_performance() something real to learn from.
                "category": product_type,
                "score": product.get("score", 0),
                # Routed by name, resolved to a Pinterest id at publish time.
                # An explicit PIN_BOARD_ID still overrides, for pinning the
                # whole account to one board deliberately.
                "board_name": board_routing.choose_board(
                    product.get("title", ""),
                    product.get("clean_title", ""),
                    product.get("category_name", ""),
                    copy["title"]),
                "board_id": self.config.buffer_board_id,
            }

            ok, reasons = self.gate.approve(pin)
            if not ok:
                logger.warning(f"Compliance rejected '{copy['title'][:40]}': "
                               + "; ".join(reasons))
                continue

            return pin

        self.last_error = "no candidate produced a compliant pin"
        logger.info(self.last_error)
        return None

    # ── advice pins ──────────────────────────────────────────────
    #
    # WHY FOUR PINS IN FIVE NOW SELL NOTHING.
    #
    # Every one of the first forty-one pins carried an affiliate link. That
    # is the shape of account Pinterest suppresses rather than removes: the
    # ratio everybody converges on is roughly 80% content that stands on its
    # own and 20% that promotes, and an account at 100% has its distribution
    # cut quietly. It fits exactly what this account saw -- forty-one pins,
    # an audience of three, and not a single save.
    #
    # So the other four pins carry a real tidying tip, a real photograph and
    # NO destination link at all. The ratio is enforced by slot rank rather
    # than by a random draw, because a draw can hand you five affiliate pins
    # in a row and the account only gets one first impression.

    # One in five carries a link.
    PRODUCT_SHARE = 5

    # How many tips are held back before one may repeat. The bank holds
    # forty-five, so seventeen are always fresh to reach for. At three advice
    # pins a day a tip comes round again after about a fortnight.
    TIP_ROTATION = 28

    # Tips to try before giving the slot up. Each attempt is one image
    # download now that the photographs are chosen rather than searched, so
    # this can be generous.
    VALUE_PIN_ATTEMPTS = 4

    def product_quota(self) -> int:
        """
        How many of today's pins may carry an affiliate link.

        Never zero. At the current cap of four this is one, which is 25%
        rather than 20% -- you cannot land on a fifth of four -- and one
        earning pin a day is the floor worth keeping.
        """
        return max(1, round(self.daily_cap() / self.PRODUCT_SHARE))

    async def wants_product_pin(self, slot: Optional[Dict] = None) -> bool:
        """
        Whether this slot should sell something.

        Decided by SLOT RANK, not by chance. The slots are ordered
        best-first, so the affiliate pin takes the strongest hour of the day
        -- 05:00 PKT, which is 8pm on the American east coast -- and the
        advice pins fill the rest. A random draw at one-in-five would put
        two affiliate pins back to back often enough to matter on an account
        this young.
        """
        quota = self.product_quota()
        rank = (slot or {}).get("rank")
        if rank:
            return rank <= quota

        # Run by hand from the dashboard, so there is no slot to place it in.
        # Ask the day instead: sell only while the day is still short of its
        # quota. Counted in the DATABASE rather than in memory, so a restart
        # cannot reset it -- the same defect that let eight pins publish
        # while pin_posts held nothing.
        published = await self.store.posted_today("product")
        return published < quota

    @staticmethod
    def _tip_id(title: str) -> str:
        """A stable id for a tip, recognisable in the table at a glance."""
        slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
        return f"tip:{slug[:56]}"

    async def _next_tip(self, tried: List[str]) -> Optional[Dict]:
        """
        A tip to publish: freshly written if possible, from the bank if not.

        WRITTEN FIRST, BANKED SECOND. Forty-five hand-made tips is eleven
        days at four advice pins a day, and a repeated pin earns nothing --
        Pinterest gives a fresh pin a distribution test for a day or two and
        gives a re-upload none at all. So the writer is the supply and the
        bank is the safety net for when Groq or the vision check cannot
        answer, which is exactly when publishing something unverified would
        be worst.
        """
        seen = self.recent_tips + tried

        if self.writer:
            board = self.writer.pick_board(self.recent_boards)
            tip = await self.writer.write(board, avoid=seen)
            if tip and not self._tip_already_used(tip["title"], seen):
                return tip
            if tip:
                logger.info(f"Written tip too close to a recent one: "
                            f"{tip['title'][:46]}")

        fallback = tip_bank.next_tip(seen)
        if fallback:
            logger.info("Using a tip from the hand-written bank.")
        return fallback

    @staticmethod
    def _tip_already_used(title: str, seen: List[str]) -> bool:
        """
        Whether a written tip repeats one published lately.

        Compared on meaningful words rather than exact text, because a model
        told to avoid a list will happily return the same advice under a
        different sentence.
        """
        words = PinAgent._title_words(title)
        if len(words) < 2:
            return True
        for previous in seen:
            other = PinAgent._title_words(previous)
            if len(other) < 2:
                continue
            common = words & other
            if len(common) >= 3 or (
                    len(common) >= 2
                    and len(common) / min(len(words), len(other)) >= 0.6):
                return True
        return False

    async def _find_photo(self, query: str) -> str:
        """
        A photograph for a freshly written tip, confirmed by looking at it.

        Returns "" rather than an unverified picture. A wrong photograph is
        worse than a missed slot: the fossil-beds result for "bed" passed
        every check that reads text about an image, and would have published.
        """
        if not (self.photos and self.verifier and self.verifier.is_ready):
            return ""

        # Exact first, then broader. "toilet shelf" returned nothing at all
        # and "medicine cabinet" returned four photographs the model refused,
        # while "bathroom" would have found plenty -- a tip about a shelf
        # above the toilet is perfectly well illustrated by a tidy bathroom.
        # Each attempt is verified against the words it searched for, so a
        # broader picture is still confirmed to show what it claims.
        tried = set()
        for attempt in (query, self._broaden(query)):
            if not attempt or attempt in tried:
                continue
            tried.add(attempt)
            candidates = await self.photos.candidates(attempt, limit=4)
            if not candidates:
                continue
            found = await self.verifier.first_approved(candidates, attempt)
            if found:
                return found
        return ""

    # Rooms, in the order a two-word query is most likely to name one.
    _ROOMS = ("bathroom", "kitchen", "pantry", "bedroom", "closet",
              "hallway", "laundry", "shower", "fridge", "cabinet", "drawer")

    @classmethod
    def _broaden(cls, query: str) -> str:
        """
        A wider version of a photo query.

        Falls back to the room the tip is about, because that is what the
        open libraries reliably hold. A specific fitting -- "toilet shelf",
        "spice drawer insert" -- often has no openly licensed photograph at
        all, while the room it lives in has thousands.
        """
        words = (query or "").lower().split()
        for room in cls._ROOMS:
            if room in words:
                return room
        return words[-1] if len(words) > 1 else ""

    async def build_value_pin(self) -> Optional[Dict]:
        """
        Produces one advice pin, or None.

        NO LINK, no #ad, and a real photograph rather than a supplier's
        product shot. If no photograph can be found the slot is given up
        rather than filled with a product pin: falling back the other way
        would quietly restore the all-affiliate feed on exactly the days the
        photo providers are down, which is the one thing this must not do.
        """
        tried: List[str] = []
        for _ in range(self.VALUE_PIN_ATTEMPTS):
            tip = await self._next_tip(tried)
            if not tip:
                self.last_error = "no tip could be produced"
                logger.error(self.last_error)
                return None
            tried.append(tip["title"])

            photo_url, credit = tip["image"], tip.get("credit", "")
            if not photo_url:
                # A freshly written tip arrives with no picture. Search for
                # one and have the vision check confirm it shows the thing
                # before it is used -- the whole reason the hand-picked bank
                # existed in the first place.
                photo_url = await self._find_photo(tip["photo"])
                if not photo_url:
                    logger.info(f"No verified photograph for "
                                f"'{tip['photo']}'; trying another tip.")
                    continue

            # The tip names its own board. Running advice through the
            # product keyword router put a tea-towel tip on the bathroom
            # board and a hallway tip on a kitchen cabinet one.
            board = tip["board"]

            image_path, image_bytes = await self.imaging.build(
                # A pseudo-product, so the imaging module needs no change:
                # it reads `images` and nothing else about the source.
                {"images": [photo_url], "title": tip["title"]},
                tip["title"], eyebrow=tip_bank.eyebrow_for(board),
                # No accent-wash fallback. An advice pin is a photograph and
                # a sentence; without the photograph there is no pin, and
                # there are forty-four other tips to try.
                require_photo=True)
            if not image_path:
                logger.info(f"The photograph for '{tip['title'][:40]}' could "
                            f"not be fetched; trying another tip.")
                continue

            image_url = ""
            if self.upload_image:
                image_url = await self.upload_image(image_path) or ""

            pin = {
                "kind": "value",
                "product_id": self._tip_id(tip["title"]),
                "title": tip["title"],
                "description": tip_bank.describe(tip, board, credit),
                # Deliberately empty, and checked as such by the gate.
                "link": "",
                "image_path": image_path,
                "image_url": image_url,
                "image_hash": self.gate.image_fingerprint(image_bytes),
                "angle": tip_bank.VALUE_ANGLE,
                "category": board,
                "score": 0,
                "board_name": board,
                "board_id": self.config.buffer_board_id,
                "photo_url": photo_url,
            }

            ok, reasons = self.gate.approve(pin)
            if not ok:
                logger.warning(f"Compliance rejected advice pin "
                               f"'{tip['title'][:40]}': " + "; ".join(reasons))
                continue

            return pin

        self.last_error = (f"no advice pin could be built from "
                           f"{self.VALUE_PIN_ATTEMPTS} tips "
                           f"(their photographs could not be fetched)")
        logger.warning(self.last_error)
        return None

    # Three missed slots is most of a day at the current volume, and long
    # enough to be certain it is not one unlucky batch.
    FAILURES_BEFORE_ALARM = 3

    async def _note_failure(self) -> None:
        """
        Says something when the agent stops producing.

        A returned None looked identical whether the market was quiet or the
        duplicate guard had jammed, and it was logged at INFO among a
        thousand other lines. So when the guard did jam, the agent published
        nothing for thirty-two hours, the website carried on normally, and
        the first anyone knew was the owner noticing an empty board.

        Silence is the one failure mode a scheduled job must never have.
        """
        self._consecutive_failures += 1
        n = self._consecutive_failures
        if n < self.FAILURES_BEFORE_ALARM:
            logger.info(f"Pin slot produced nothing ({n} in a row): "
                        f"{self.last_error}")
            return

        logger.error(f"PIN AGENT HAS PRODUCED NOTHING FOR {n} SLOTS IN A ROW. "
                     f"Last reason: {self.last_error}")
        if not self.nm or n != self.FAILURES_BEFORE_ALARM:
            return                      # Alert once, not on every slot after.
        try:
            await self.nm.send_notification(
                subject=f"Pinterest agent has published nothing for {n} slots",
                message=(f"The pin agent has failed {n} slots in a row.\n\n"
                         f"Last reason: {self.last_error}\n\n"
                         f"Published today: {self.published_today}/"
                         f"{self.daily_cap()}\n"
                         f"Products already used: {len(self.gate.seen_products)}\n"
                         f"Recent titles held: {len(self.recent_titles)}\n\n"
                         f"A run of 'no candidate produced a compliant pin' "
                         f"usually means a filter has become too strict for "
                         f"the number of products the niche returns."),
                is_critical=True)
        except Exception as e:
            logger.warning(f"Could not send the pin alarm: {type(e).__name__}")

    async def run_once(self, slot: Optional[Dict] = None) -> Optional[Dict]:
        """
        One cycle: build a pin, then either queue it for review or publish it.

        `slot` is the schedule slot this run belongs to, and it decides which
        SORT of pin gets built -- see wants_product_pin. Called without one
        (the dashboard's Run now), the day's counts decide instead.

        Returns the pin, or None if nothing was produced.
        """
        self.last_run = datetime.now(timezone.utc)

        if self.published_today >= self.daily_cap():
            logger.info(f"Daily pin limit reached "
                        f"({self.published_today}/{self.config.pins_per_day}).")
            return None

        if await self.wants_product_pin(slot):
            pin = await self.build_one()
            # THE FALLBACK ONLY RUNS THIS WAY ROUND.
            #
            # A dry sourcing call or a batch where nothing passes the filters
            # used to cost the slot entirely. An advice pin needs neither
            # AliExpress nor a product, so the slot is still worth filling --
            # and erring towards the pin that sells nothing can only improve
            # the ratio, never breach it. The reverse fallback does not
            # exist, deliberately.
            if not pin:
                logger.info(f"No product pin this slot ({self.last_error}); "
                            f"publishing advice instead.")
                pin = await self.build_value_pin()
        else:
            pin = await self.build_value_pin()

        if not pin:
            await self._note_failure()
            return None
        self._consecutive_failures = 0

        if self.config.require_review:
            pin["status"] = "awaiting_review"
            self.pending_review.append(pin)
            await self.store.save_pin(pin)
            pin["_recorded"] = True
            # Suppressed the moment it is queued, not when it publishes.
            #
            # remember() was only called on a successful publish or an
            # explicit rejection, so a pin waiting for a decision left its
            # product free to be picked again -- and it was: the egg
            # organiser 1005008248056658 was built twice, six seconds apart,
            # under two different titles. A restart hid the bug, because
            # posted_history() reloads every row regardless of status; it
            # only showed inside a single running session.
            self.gate.remember(pin["product_id"], pin["link"],
                               pin.get("image_hash", ""))
            self._remember(pin)
            logger.info(f"Pin awaiting review: '{pin['title'][:50]}'")
            await self._notify_review(pin)
            return pin

        return await self.publish(pin)

    def _remember(self, pin: Dict) -> None:
        """
        Holds a just-handled pin in memory until the next connect() reload.

        THE TWO HISTORIES ARE SEPARATE. An advice pin's title is a
        hand-written sentence about tidying and shares the whole generic
        vocabulary of the niche; dropping it into the product duplicate
        guard would spend a slot of real product history on text no product
        can be compared against, and four pins in five are now advice pins.

        Both lists are bounded here as well. They were not, and on a long
        run the product guard compared every candidate against every title
        of the session rather than the recent forty.
        """
        if self.gate.kind_of(pin) == "value":
            self.recent_tips.insert(0, pin.get("title", ""))
            del self.recent_tips[self.TIP_ROTATION:]
            board = pin.get("board_name") or ""
            if board:
                self.recent_boards.insert(0, board)
                del self.recent_boards[24:]
            return

        self.recent_titles.insert(
            0, f"{pin.get('title', '')} {pin.get('description', '')}")
        del self.recent_titles[self.RECENT_TITLE_COUNT:]

        # And the subject, so two spice racks cannot go out in one session
        # before the next connect() reloads the window from the database.
        kind = pin.get("category") or ""
        if kind:
            self.recent_types.insert(0, kind)
            del self.recent_types[400:]

    async def publish(self, pin: Dict) -> Optional[Dict]:
        """Publishes an already-approved pin and records the outcome."""
        # Re-checked rather than trusted: a pin may have sat in the review
        # queue for hours, and the gate is cheap.
        ok, reasons = self.gate.approve(pin)
        if not ok:
            logger.error(f"Pin failed compliance at publish time: "
                         + "; ".join(reasons))
            pin["status"] = "rejected"
            return None

        sent = await self.publisher.publish(pin)
        pin["status"] = "published" if sent else "failed"

        # WRITE THE ROW, do not merely update one.
        #
        # save_pin() was only called in the review branch, so with review
        # OFF nothing was ever inserted and mark_status() patched a row that
        # did not exist. Eight pins were live on Pinterest while pin_posts
        # held zero published records -- and everything that reads that
        # table was quietly broken with it: the duplicate guard had no
        # history to compare against, the daily cap counted zero, the ramp
        # believed it was day zero forever, and the slot guard could never
        # see its own work.
        if pin.get("_recorded"):
            await self.store.mark_status(pin["product_id"], pin["status"])
        else:
            await self.store.save_pin(pin)
            pin["_recorded"] = True

        if sent:
            self.published_today += 1
            self.gate.remember(pin["product_id"], pin["link"], pin["image_hash"])
            # Kept in memory too, so two pins in the same session cannot
            # describe the same product before the next connect() reload.
            self._remember(pin)
            logger.info(f"Pin published ({self.published_today}/"
                        f"{self.daily_cap()} today, "
                        f"{self.gate.kind_of(pin)}).")
        else:
            self.last_error = self.publisher.last_error

        return pin if sent else None

    # ── review queue ─────────────────────────────────────────────

    async def _notify_review(self, pin: Dict) -> None:
        """
        Asks for a decision by email.

        A light human check is worth keeping for the first weeks: this
        recommends purchases rather than opinions, so catching an invented
        feature before it publishes is cheap insurance.
        """
        if not self.nm:
            return
        await self.nm.send_notification(
            subject=f"Pin awaiting review: {pin['title'][:60]}",
            message=(f"{pin['title']}\n\n{pin['description']}\n\n"
                     f"Product: {pin['product_id']}\n"
                     f"Link: {pin['link']}\n"
                     f"Image: {pin.get('image_url') or pin.get('image_path')}\n\n"
                     f"Approve or reject it from the Novi dashboard."),
            is_critical=False,
        )

    async def approve_pending(self, product_id: str = "") -> Optional[Dict]:
        """Publishes a reviewed pin. With no id, takes the oldest waiting."""
        if not self.pending_review:
            return None
        pin = next((p for p in self.pending_review
                    if not product_id or p["product_id"] == product_id), None)
        if not pin:
            return None
        self.pending_review.remove(pin)
        return await self.publish(pin)

    def reject_pending(self, product_id: str = "") -> bool:
        """Drops a pin, and blocks the product from being picked again."""
        pin = next((p for p in self.pending_review
                    if not product_id or p["product_id"] == product_id), None)
        if not pin:
            return False
        self.pending_review.remove(pin)
        self.gate.remember(pin["product_id"], pin["link"], pin["image_hash"])
        logger.info(f"Pin rejected and product suppressed: {pin['title'][:44]}")
        return True

    def reset_daily(self) -> None:
        self.published_today = 0
