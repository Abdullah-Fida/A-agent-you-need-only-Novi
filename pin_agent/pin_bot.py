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
from pin_agent.compliance import ComplianceGate
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
                 upload_image=None):
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
        # Titles of recent pins, so the same product from a different
        # seller is not pinned twice days apart.
        self.recent_titles: List[str] = []
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
            "days_live": self.days_live,
            "awaiting_review": len(self.pending_review),
            "review_required": self.config.require_review,
            "last_run": self.last_run.isoformat() if self.last_run else None,
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
    RAMP = ((10, 4), (20, 6), (30, 8), (45, 11))
    RAMP_CEILING = 15

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

    # Titles are compared against roughly a fortnight of pins. Beyond that
    # the board has moved on and a repeat is fair.
    RECENT_TITLE_COUNT = 40

    _TITLE_NOISE = {
        "this", "that", "the", "a", "an", "and", "or", "with", "for", "your",
        "you", "keep", "make", "makes", "made", "from", "into", "onto", "any",
        "every", "all", "one", "two", "get", "gets", "have", "has", "its",
        "it", "in", "on", "of", "to", "is", "are", "up", "out", "off", "at",
        "by", "no", "so", "can", "will", "perfect", "great", "ideal", "best",
        "kitchen", "home", "space", "saving", "saver", "small", "clear",
        "tidy", "organized", "organised", "organizer", "storage",
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
            # TWO conditions, not one. A ratio alone collapses when a title
            # reduces to a single meaningful word: "kitchen storage
            # organizer for small space saving homes" leaves just {homes},
            # and one shared word out of one is a perfect score. Requiring
            # two real words in common as well is what separates the same
            # product from the same vocabulary.
            if len(common) < 2:
                continue
            if len(common) / min(len(words), len(other)) >= self.TITLE_OVERLAP:
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
            self.RECENT_TITLE_COUNT)

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
            if self._too_similar_to_recent(copy["title"]):
                logger.info(f"Skipping '{copy['title'][:44]}' — too close to "
                            f"something pinned recently.")
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
                "category": product.get("category_name", ""),
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

    async def run_once(self) -> Optional[Dict]:
        """
        One cycle: build a pin, then either queue it for review or publish it.

        Returns the pin, or None if nothing was produced.
        """
        self.last_run = datetime.now(timezone.utc)

        if self.published_today >= self.daily_cap():
            logger.info(f"Daily pin limit reached "
                        f"({self.published_today}/{self.config.pins_per_day}).")
            return None

        pin = await self.build_one()
        if not pin:
            return None

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
            self.recent_titles.insert(0, pin["title"])
            logger.info(f"Pin awaiting review: '{pin['title'][:50]}'")
            await self._notify_review(pin)
            return pin

        return await self.publish(pin)

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
            self.recent_titles.insert(0, pin.get("title", ""))
            logger.info(f"Pin published ({self.published_today}/"
                        f"{self.daily_cap()} today).")
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
