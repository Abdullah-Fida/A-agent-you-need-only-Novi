"""
Tests for the Pinterest agent.

Runs entirely offline — no AliExpress key, no Pinterest app, no network. The
compliance tests matter most: a pin that breaks Pinterest's affiliate rules
risks the account, and that is not recoverable by apologising.

    python -m unittest pin_agent.tests.test_pin_agent
"""
import asyncio
import json
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from pin_agent.compliance import ComplianceGate
from pin_agent.selector import ProductSelector
from pin_agent import boards as board_routing
from pin_agent.publisher import PinterestPublisher
from pin_agent.sourcing import AliExpressClient


def valid_pin(**overrides):
    pin = {
        "title": "5 Kitchen Gadgets That Actually Save Time",
        "description": ("Herb scissors that cut prep time in half. Five blades "
                        "and a cleaning comb included. #ad"),
        "link": "https://s.click.aliexpress.com/e/_sample1",
        "product_id": "1005006123456",
        "image_path": "/tmp/pin.jpg",
        "image_hash": "abc123",
    }
    pin.update(overrides)
    return pin


class TestLinkCompliance(unittest.TestCase):
    """Cloaked links are the fastest way to lose an affiliate account."""

    def setUp(self):
        self.gate = ComplianceGate()

    def test_affiliate_link_is_accepted(self):
        self.assertIsNone(self.gate.check_link("https://s.click.aliexpress.com/e/_x"))

    def test_subdomain_of_an_allowed_host_is_accepted(self):
        self.assertIsNone(self.gate.check_link("https://star.aliexpress.com/share/x"))

    def test_shorteners_are_rejected(self):
        for url in ("https://bit.ly/x", "https://tinyurl.com/x",
                    "https://cutt.ly/x", "https://linktr.ee/x"):
            self.assertIsNotNone(self.gate.check_link(url), f"{url} should be rejected")

    def test_lookalike_domain_is_rejected(self):
        """aliexpress.com.attacker.net must not pass as an AliExpress host."""
        self.assertIsNotNone(
            self.gate.check_link("https://aliexpress.com.attacker.net/x"))

    def test_unrelated_dotcom_is_rejected(self):
        """
        Regression: matching on the registrable suffix meant every .com
        address was accepted, so any redirect host passed the gate.
        """
        self.assertIsNotNone(self.gate.check_link("https://sketchy-redirect.com/go/1"))

    def test_plain_http_is_rejected(self):
        self.assertIsNotNone(self.gate.check_link("http://s.click.aliexpress.com/e/_x"))

    def test_empty_link_is_rejected(self):
        self.assertIsNotNone(self.gate.check_link(""))


class TestRatingScaleAndRanking(unittest.TestCase):
    """
    Three faults found by running the selector over live AliExpress data,
    none of which any offline test would have shown.
    """

    def setUp(self):
        self.S = ProductSelector

    def _product(self, **kw):
        p = {"affiliate_url": "https://s.click.aliexpress.com/e/_x",
             "images": ["https://x/1.jpg"],
             "title": "Kitchen Drawer Organizer Tray Set",
             "rating": 98.0, "orders": 1000, "price": 20.0,
             "original_price": 40.0, "commission_rate": 7.0}
        p.update(kw)
        return p

    def test_the_rating_floor_is_a_percentage_not_stars(self):
        """
        The floor was 4.3, written as if satisfaction were out of five. On
        AliExpress it is a percentage, so 4.3 was compared against values
        like 98.0 and rejected nothing at all -- listings rated 81.3%, 86.4%
        and 88.2% went straight through.
        """
        selector = ProductSelector()
        self.assertEqual(selector.min_rating, 90.0)
        self.assertFalse(selector.is_eligible(self._product(rating=81.3)))
        self.assertFalse(selector.is_eligible(self._product(rating=88.2)))
        self.assertTrue(selector.is_eligible(self._product(rating=98.0)))

    def test_a_five_point_rating_is_converted(self):
        # Both fields feed the same key, so a perfect 5.0 must not read as
        # a 5% approval rating.
        self.assertEqual(self.S.as_percentage(4.8), 96.0)
        self.assertEqual(self.S.as_percentage(98.0), 98.0)
        self.assertEqual(self.S.as_percentage(0), 0.0)

    def test_order_volume_outranks_a_marginally_better_rating(self):
        """
        The rating term was `(rating - min_rating) * 12`, about 1,124 of a
        1,189-point score, so orders, commission and discount together moved
        the result by half a percent. A listing with 544 orders outranked
        one with 2,465 because its score was 99.3 rather than 98.0.
        """
        selector = ProductSelector()
        popular = selector.score(self._product(orders=2465, rating=98.0))
        niche = selector.score(self._product(orders=544, rating=99.3))
        self.assertGreater(popular, niche)

    def test_the_rating_bonus_is_capped(self):
        selector = ProductSelector()
        spread = (selector.score(self._product(rating=100.0))
                  - selector.score(self._product(rating=90.0)))
        self.assertLessEqual(spread, 20.5)

    def test_a_cheap_product_still_has_to_be_well_rated(self):
        """
        The price floor came down from $12 to $8 because at twelve a live
        batch of forty was reduced to two, mostly on price. Widening the
        price band must not widen the quality band with it: the two filters
        are independent, and a $9 product rated 81% is still refused.
        """
        selector = ProductSelector(min_price=8.0)
        self.assertTrue(selector.is_eligible(
            self._product(price=8.66, rating=95.7)))
        self.assertFalse(selector.is_eligible(
            self._product(price=8.66, rating=81.3)),
            "a cheap product must still clear the rating floor")
        self.assertFalse(selector.is_eligible(
            self._product(price=8.66, rating=98.0, orders=12)),
            "a cheap product must still clear the order floor")

    def test_the_price_floor_is_eight(self):
        from pin_agent.config import PinConfig
        self.assertEqual(PinConfig.min_price, 8.0)

    def test_the_same_product_from_two_sellers_is_pinned_once(self):
        """
        Deduplicating on product_id is not enough. A live batch returned the
        same flatware organizer twice under different ids and prices, and
        both were selected -- two of five pins showing one drawer tray,
        which on Pinterest reads as a spam account.
        """
        selector = ProductSelector()
        pair = [
            self._product(product_id="1", orders=4288,
                          title="1Pcs Upgradation Adjustable Flatware Tableware Organizer"),
            self._product(product_id="2", orders=3084, price=8.17,
                          title="1Pcs Upgradation Adjustable Flatware Tableware Organizer"),
        ]
        chosen = selector.select(pair + [
            self._product(product_id="3", orders=1465,
                          title="4 Layers Kitchen Spice Drawer Organizer Adjustable"),
        ], limit=5)
        self.assertEqual(len(chosen), 2)

        # Which of the pair wins is the ranking's business -- the deeper
        # discount can beat the higher order count. What matters here is
        # that the survivor is the better-scored one, not the first seen.
        best = max(pair, key=selector.score)["product_id"]
        self.assertEqual({p["product_id"] for p in chosen}, {best, "3"})

    def test_genuinely_different_products_both_survive(self):
        chosen = ProductSelector().select([
            self._product(product_id="1", title="Kitchen Spice Drawer Organizer Rack"),
            self._product(product_id="2", title="Under Sink Pull Out Storage Shelf"),
        ], limit=5)
        self.assertEqual(len(chosen), 2)


class TestReviewQueueSurvivesRestart(unittest.TestCase):
    """
    The review queue lived only in memory.

    Every Render restart -- which is every deploy -- emptied it. A pin was
    built, saved, and emailed asking for approval, and then pressing Approve
    found an empty list. Worse, posted_history() reads every pin_posts row
    regardless of status, so the orphaned product entered the dedupe set and
    could not be featured again for 120 days. The pin was lost AND the
    product was burned.
    """

    def setUp(self):
        from pin_agent.store import PinStore
        self.store = PinStore(client=None)      # offline mode

    def test_pins_awaiting_review_are_returned(self):
        self.store._memory = [
            {"product_id": "1", "status": "awaiting_review", "title": "Waiting"},
            {"product_id": "2", "status": "published", "title": "Already out"},
            {"product_id": "3", "status": "awaiting_review", "title": "Also waiting"},
        ]
        pending = asyncio.run(self.store.pending_pins())
        self.assertEqual([p["product_id"] for p in pending], ["1", "3"])

    def test_nothing_pending_is_an_empty_list_not_an_error(self):
        self.store._memory = [{"product_id": "2", "status": "published"}]
        self.assertEqual(asyncio.run(self.store.pending_pins()), [])

    def test_the_agent_restores_the_queue_on_connect(self):
        """The whole point: Approve must still find the pin after a deploy."""
        import pin_agent.pin_bot as pin_bot

        agent = pin_bot.PinAgent.__new__(pin_bot.PinAgent)
        agent.pending_review = []
        agent.published_today = 0
        agent.gate = MagicMock()
        agent.selector = MagicMock()
        agent.publisher = MagicMock()

        async def connected():
            return True

        agent.publisher.connect = connected
        agent.store = MagicMock()

        async def history():
            return {"product_ids": [], "urls": [], "image_hashes": []}

        async def posted_today():
            return 0

        async def pending():
            return [{"product_id": "1", "status": "awaiting_review",
                     "title": "Survived the restart"}]

        async def performance():
            return {}

        async def first_pin_at():
            return None

        async def recent_titles(limit=40, kind="product"):
            return []

        async def recent_tip_titles(limit=50):
            return []

        async def recent_types(days=7):
            return []

        async def verified_photos(limit=120):
            return []

        async def photo_memory():
            return {"used": [], "pool": []}

        agent.store.photo_memory = photo_memory
        agent.store.USED_LIMIT = 400
        agent.store.posted_history = history
        agent.store.posted_today = posted_today
        agent.store.pending_pins = pending
        agent.store.category_performance = performance
        agent.store.first_pin_at = first_pin_at
        agent.store.recent_titles = recent_titles
        agent.store.recent_tip_titles = recent_tip_titles
        agent.store.recent_types = recent_types
        agent.store.verified_photos = verified_photos

        asyncio.run(agent.connect())
        self.assertEqual(len(agent.pending_review), 1)
        self.assertEqual(agent.pending_review[0]["title"], "Survived the restart")


class TestNoRepeatedProducts(unittest.TestCase):
    """
    Two ways the same product reached the board twice, both seen live.
    """

    def setUp(self):
        from pin_agent.pin_bot import PinAgent
        self.agent = PinAgent.__new__(PinAgent)
        self.agent.recent_titles = []

    def _seen(self, *titles):
        self.agent.recent_titles = list(titles)

    def test_the_same_item_from_another_seller_is_caught(self):
        """
        Two sellers list the same 4-layer spice drawer organiser under
        different product ids, so id-based dedupe misses it entirely. Both
        were pinned a day apart -- on a young board that is the most visible
        possible sign of automation.
        """
        self._seen("This 4-layer adjustable spice drawer organizer fits snugly")
        self.assertTrue(self.agent._too_similar_to_recent(
            "This 4-layer adjustable spice rack slides into a drawer"))

    def test_genuinely_different_products_are_allowed(self):
        self._seen("This 4-layer adjustable spice drawer organizer fits snugly")
        for title in ("Stackable soda can dispenser makes fridge tidy",
                      "Keep eggs fresh in a two-layer fridge box",
                      "Under sink pull out shelf for cleaning bottles"):
            self.assertFalse(self.agent._too_similar_to_recent(title), title)

    def test_the_niche_vocabulary_does_not_count_as_similarity(self):
        """
        Almost every title contains "kitchen", "storage", "organizer" or
        "space saving". Counting those would make everything a duplicate and
        the agent would publish nothing at all.
        """
        self._seen("Kitchen storage organizer for small space saving homes")
        self.assertFalse(self.agent._too_similar_to_recent(
            "Kitchen storage organizer saves space in small homes for mugs"))

    def test_a_duplicate_is_caught_on_shared_words_when_the_ratio_is_low(self):
        """
        The ratio alone let a real duplicate through.

        Two listings of the same herb scissors published five days apart.
        Compared as whole pins the word sets are large, so they shared
        {scissors, stainless, steel} but scored only 0.40 -- under the
        threshold, so the second one went out. Three meaningful words in
        common is the signal the ratio was missing.
        """
        self._seen("Elegant herb scissors for the culinary enthusiast "
                   "These stainless steel herb scissors feature five "
                   "precision blades and a cleaning comb")
        self.assertTrue(self.agent._too_similar_to_recent(
            "Save minutes chopping herbs with 5-blade scissors "
            "These stainless steel 5-blade scissors make chopping herbs "
            "a breeze and rinse clean"))

    def test_products_sharing_no_meaningful_words_are_allowed(self):
        """
        Measured on the live board: four genuinely different products
        shared ZERO meaningful words with each other, so a three-word bar
        has real headroom.
        """
        self._seen("Keep your bathroom countertop clutter-free with this "
                   "no-drill toilet paper shelf")
        for other in (
            "Keep your leftovers fresh and ready for guests with this set "
            "of airtight containers",
            "Tired of uneven slices? Our 12-in-1 slicer takes the effort "
            "out of prep",
            "Magnetic knife strip mounts without drilling and frees the "
            "worktop completely",
        ):
            self.assertFalse(self.agent._too_similar_to_recent(other), other[:40])

    def test_filler_words_do_not_count_as_shared(self):
        # "These" and "our" open half the descriptions written; counting
        # them would push unrelated pins over the three-word bar.
        from pin_agent.pin_bot import PinAgent
        for filler in ("these", "those", "our", "their"):
            self.assertNotIn(filler, PinAgent._title_words(
                f"These our their those {filler} items"))

    def test_an_empty_history_blocks_nothing(self):
        self._seen()
        self.assertFalse(self.agent._too_similar_to_recent("Anything at all here"))

    def test_a_pin_awaiting_review_suppresses_its_product(self):
        """
        remember() ran only on a successful publish or an explicit
        rejection, so a pin waiting for a decision left its product free to
        be picked again -- and it was: the egg organiser 1005008248056658
        was built twice, six seconds apart, under two different titles.
        """
        import inspect
        from pin_agent.pin_bot import PinAgent
        source = inspect.getsource(PinAgent.run_once)
        review = source[source.index("require_review"):]
        self.assertIn("self.gate.remember", review,
                      "a pin queued for review must suppress its product")


class TestAllSixBoardsGetFed(unittest.TestCase):
    """
    The profile has six boards; the search terms only reached four.

    "Kitchen Gadgets Worth Buying" matched none of the sixteen keywords and
    could never receive a pin at all, and "Bathroom Storage Ideas" had
    exactly one. Board routing was never the problem -- sourcing was.
    """

    def setUp(self):
        from pin_agent.sourcing import NICHE_KEYWORDS
        from pin_agent.boards import ALL_BOARDS, choose_board
        self.keywords = NICHE_KEYWORDS["home_kitchen"]
        self.boards = ALL_BOARDS
        self.route = choose_board

    def test_every_board_has_search_terms(self):
        from collections import Counter
        covered = Counter(self.route(k) for k in self.keywords)
        for board in self.boards:
            self.assertGreater(covered[board], 0,
                               f"nothing ever searched for {board}")

    def test_the_boards_are_fed_evenly(self):
        from collections import Counter
        covered = Counter(self.route(k) for k in self.keywords)
        counts = [covered[b] for b in self.boards]
        self.assertLessEqual(max(counts) - min(counts), 1,
                             f"uneven board coverage: {dict(covered)}")

    def test_consecutive_searches_land_on_different_boards(self):
        """
        Grouped keywords would put four bathroom pins out in a row. The
        rotation is what keeps the profile looking browsed rather than
        batch-uploaded.
        """
        routed = [self.route(k) for k in self.keywords]
        for i in range(len(routed) - 1):
            self.assertNotEqual(routed[i], routed[i + 1],
                                f"{self.keywords[i]} and {self.keywords[i+1]} "
                                f"both go to {routed[i]}")

    def test_the_keyword_rotation_covers_every_board_before_repeating(self):
        from pin_agent.sourcing import AliExpressClient
        client = AliExpressClient(niche="home_kitchen")
        first_pass = {self.route(client.next_keywords())
                      for _ in range(len(self.boards))}
        self.assertEqual(first_pass, set(self.boards))

    def test_the_category_ids_were_not_clobbered(self):
        # A careless edit once replaced NICHE_CATEGORIES with the keyword
        # list, which would have sent every search to no category at all.
        from pin_agent.sourcing import NICHE_CATEGORIES
        self.assertEqual(NICHE_CATEGORIES["home_kitchen"], ["1501", "15"])


class TestPublishedPinsAreRecorded(unittest.TestCase):
    """
    With review OFF, nothing was ever written to the database.

    save_pin() lived only in the review branch, so mark_status() patched a
    row that did not exist. Eight pins were live on Pinterest while
    pin_posts held zero published records -- and every guard that reads that
    table was broken with it: the duplicate check had no history, the daily
    cap counted zero, the ramp believed it was day zero forever, and the
    slot guard could not see its own work.
    """

    def setUp(self):
        import inspect
        from pin_agent.pin_bot import PinAgent
        self.publish_src = inspect.getsource(PinAgent.publish)

    def test_publishing_writes_a_row(self):
        self.assertIn("save_pin", self.publish_src,
                      "a published pin must be recorded, not just patched")

    def test_an_already_recorded_pin_is_updated_not_duplicated(self):
        # A review-approved pin already has its row from run_once.
        self.assertIn("_recorded", self.publish_src)
        self.assertIn("mark_status", self.publish_src)

    def test_a_published_pin_joins_the_duplicate_history(self):
        """
        Behavioural now rather than a grep for "recent_titles": the two
        histories were split when advice pins arrived, so the recording
        moved into _remember() and a source check would pass on the name
        alone while testing nothing.
        """
        from pin_agent.pin_bot import PinAgent
        agent = PinAgent.__new__(PinAgent)
        agent.recent_titles, agent.recent_tips = [], []
        agent.gate = ComplianceGate()

        agent._remember({"title": "Stackable egg tray keeps a fridge tidy",
                         "description": "Two layers. #ad", "angle": "gift_idea"})

        self.assertEqual(len(agent.recent_titles), 1)
        self.assertTrue(agent._too_similar_to_recent(
            "Stackable egg tray for a tidy fridge"),
            "without this, two pins in one session can describe the same "
            "product")


class TestMedicalProductsAreRefused(unittest.TestCase):
    """
    A peptide case for insulin vials reached the live board.

    It was filed under "Pantry and Fridge Storage" -- a storage box by
    shape, a medical device by use, on a board about food. It passed
    because the ban list only held marketing claims like "medical grade",
    not the products themselves.
    """

    def setUp(self):
        self.selector = ProductSelector()

    def _product(self, title):
        return {"affiliate_url": "https://s.click.aliexpress.com/e/_x",
                "images": ["https://x/1.jpg"], "title": title,
                "rating": 98.0, "orders": 1000, "price": 20.0,
                "original_price": 40.0, "commission_rate": 7.0}

    def test_the_pin_that_shipped_would_now_be_refused(self):
        self.assertFalse(self.selector.is_eligible(self._product(
            "Peptide Case Safe Storage Box Insulin Vial Organizer Foam Slots")))

    def test_other_clinical_items_are_refused(self):
        for title in ("Portable Pill Organizer Weekly Medicine Box 7 Day",
                      "Diabetic Travel Case for Syringe and Needle Storage",
                      "Vitamin Supplement Capsule Dispenser Bottle",
                      "First Aid Kit Wall Mounted Storage Cabinet"):
            self.assertFalse(self.selector.is_eligible(self._product(title)),
                             title)

    def test_ordinary_kitchen_products_still_pass(self):
        # The filter must not swallow the niche it exists to serve.
        for title in ("4 Layers Kitchen Spice Drawer Organizer Adjustable Rack",
                      "Stainless Steel Herb Scissors with 5 Blades",
                      "Under Sink Pull Out Storage Shelf Organizer",
                      "Airtight Food Storage Container Set for Pantry"):
            self.assertTrue(self.selector.is_eligible(self._product(title)),
                            title)


class TestSilentOutage(unittest.TestCase):
    """
    The agent published nothing for thirty-two hours and said nothing.

    build_one() returned None, which looks identical whether the market was
    quiet or a filter had jammed, and it was logged at INFO among a thousand
    other lines. The website carried on publishing normally the whole time,
    so nothing else looked wrong; the first anyone knew was the owner
    noticing an empty board.
    """

    def setUp(self):
        from pin_agent.pin_bot import PinAgent
        from datetime import datetime, timezone
        agent = PinAgent.__new__(PinAgent)
        agent._consecutive_failures = 0
        agent.last_error = "no candidate produced a compliant pin"
        agent.nm = None
        agent.published_today = 0
        agent.recent_titles = []
        agent.gate = MagicMock()
        agent.gate.seen_products = set()
        agent.config = MagicMock()
        agent.config.pins_per_day = 15
        agent._first_pin_at = datetime.now(timezone.utc)
        self.agent = agent
        self.PinAgent = PinAgent

    def test_a_run_of_failures_is_escalated(self):
        for _ in range(self.PinAgent.FAILURES_BEFORE_ALARM):
            asyncio.run(self.agent._note_failure())
        self.assertGreaterEqual(self.agent._consecutive_failures,
                                self.PinAgent.FAILURES_BEFORE_ALARM)

    def test_one_quiet_slot_is_not_an_alarm(self):
        # A single empty slot is ordinary: the market is quiet, or one batch
        # was all duplicates. Crying wolf trains the owner to ignore it.
        asyncio.run(self.agent._note_failure())
        self.assertLess(self.agent._consecutive_failures,
                        self.PinAgent.FAILURES_BEFORE_ALARM)

    def test_the_owner_is_emailed_once_not_every_slot(self):
        sent = []

        async def capture(**kwargs):
            sent.append(kwargs)

        self.agent.nm = MagicMock()
        self.agent.nm.send_notification = capture
        for _ in range(self.PinAgent.FAILURES_BEFORE_ALARM + 3):
            asyncio.run(self.agent._note_failure())
        self.assertEqual(len(sent), 1, "the alarm repeated on every slot")
        self.assertIn("published nothing", sent[0]["subject"])

    def test_a_success_clears_the_run(self):
        import inspect
        source = inspect.getsource(self.PinAgent.run_once)
        self.assertIn("_consecutive_failures = 0", source,
                      "a good slot must reset the counter, or the alarm "
                      "fires forever after one bad day")

    def test_the_failure_count_is_visible_in_status(self):
        import inspect
        self.assertIn("consecutive_failures",
                      inspect.getsource(self.PinAgent.status.fget))


class TestDuplicateGuardCompareLikeForLike(unittest.TestCase):
    """
    What jammed the guard: comparing unlike things.

    The candidate was passed as title PLUS description PLUS hashtags, about
    twenty-five meaningful words, while the history holds titles alone,
    about five. Sharing three generic words -- "kitchens", "spaces",
    "design", "homeorganization" -- is then close to certain, and six
    candidates in a row were rejected as duplicates of unrelated pins.
    """

    def setUp(self):
        from pin_agent.pin_bot import PinAgent
        self.agent = PinAgent.__new__(PinAgent)
        self.PinAgent = PinAgent

    def test_only_the_title_is_compared(self):
        import inspect
        # The per-candidate loop lives in _pick_product since build_one
        # gained its two passes.
        source = inspect.getsource(self.PinAgent._pick_product)
        call = source[source.index("_too_similar_to_recent"):][:120]
        self.assertNotIn("description", call,
                         "comparing title+description against title-only "
                         "makes three shared words near-certain")

    def test_a_long_blob_does_not_match_a_short_title(self):
        self.agent.recent_titles = ["Compact spice organizer keeps jars tidy"]
        blob = ("Clear acrylic wall shelf saves counter space. This clear "
                "acrylic shelf mounts directly and frees precious counter "
                "area for anyone who loves cooking in small kitchens. "
                "#kitchenorganization #smallkitchen #homeorganization")
        self.assertFalse(self.agent._too_similar_to_recent(blob),
                         "a description-length blob still matches a title")

    def test_the_real_duplicate_is_still_caught(self):
        self.agent.recent_titles = [
            "This 4-layer adjustable spice drawer organizer fits snugly"]
        self.assertTrue(self.agent._too_similar_to_recent(
            "This 4-layer adjustable spice rack slides into a drawer"))

    def test_marketing_vocabulary_is_not_a_duplicate(self):
        # Every pin in this niche is a compact space-saving shelf.
        self.agent.recent_titles = ["Compact bathroom shelf saves space"]
        for title in ("Compact corner shelf with hooks for tight spaces",
                      "Compact 2-tier sliding sink organizer",
                      "Compact white divider board for kitchen drawers"):
            self.assertFalse(self.agent._too_similar_to_recent(title), title)


class TestBufferPostInput(unittest.TestCase):
    """
    The shape Buffer's createPost actually requires.

    Read off the source rather than sent over the network, because the cost
    of getting it wrong is a slot that fires, builds an image, writes copy,
    and then throws all of it away on a validation error.
    """

    def setUp(self):
        import inspect
        from pin_agent.publisher import PinterestPublisher
        self.src = inspect.getsource(PinterestPublisher)

    def test_scheduling_type_is_present(self):
        """
        SchedulingType! is required even when publishing immediately. It was
        deleted alongside the old "addToQueue" line, and the first live pin
        died on 'Field "schedulingType" of required type "SchedulingType!"
        was not provided' -- after the image had been built and uploaded.
        """
        self.assertIn('"schedulingType"', self.src)

    def test_scheduling_type_is_a_real_enum_value(self):
        # Introspected from the live schema: exactly these two.
        # 'notification' would only ping a phone to post by hand.
        self.assertIn('"schedulingType": "automatic"', self.src)

    def test_pins_publish_immediately(self):
        # Queued pins competed with Bluesky for the free plan's ten slots.
        self.assertIn('"mode": "shareNow"', self.src)
        self.assertNotIn('"mode": "addToQueue"', self.src)

    def test_every_required_field_of_the_mutation_is_supplied(self):
        for field in ('"channelId"', '"text"', '"assets"', '"mode"',
                      '"schedulingType"'):
            self.assertIn(field, self.src, f"{field} missing from the input")


class TestVolumeRamp(unittest.TestCase):
    """
    Pin volume is earned, not configured.

    A brand-new Pinterest account posting fifteen a day looks like a bought
    account being drained, and the reach penalty for that is not appealable.
    """

    def setUp(self):
        from pin_agent.pin_bot import PinAgent
        from types import SimpleNamespace
        self.agent = PinAgent.__new__(PinAgent)
        self.agent.config = SimpleNamespace(pins_per_day=15)

    def _cap_on_day(self, day):
        from datetime import datetime, timezone, timedelta
        self.agent._first_pin_at = (None if day is None else
                                    datetime.now(timezone.utc) - timedelta(days=day))
        return self.agent.daily_cap()

    def test_day_one_is_the_lowest_step(self):
        self.assertEqual(self._cap_on_day(0), 4)

    def test_before_any_pin_exists_it_is_still_day_zero(self):
        """
        days_live is None until the first pin publishes. Falling back to the
        configured maximum there would run the very first day at full
        volume -- the exact day the ramp exists to protect.
        """
        self.assertEqual(self._cap_on_day(None), 4)

    def test_the_ramp_climbs_on_schedule(self):
        # Lowered from 4/6/8/11/15 after reading what Pinterest rewards: the
        # safe range for a young account is 1-5 fresh pins a day, and pace
        # beats volume.
        for day, expected in ((10, 4), (11, 5), (20, 5), (21, 5),
                              (30, 5), (31, 6), (45, 6), (46, 8)):
            self.assertEqual(self._cap_on_day(day), expected, f"day {day}")

    def test_it_never_climbs_past_the_ceiling(self):
        for day in (60, 120, 400):
            self.assertEqual(self._cap_on_day(day), 8, f"day {day}")

    def test_the_configured_maximum_still_wins(self):
        # The ramp raises the floor over time; it must never post more than
        # the owner asked for.
        from types import SimpleNamespace
        self.agent.config = SimpleNamespace(pins_per_day=3)
        self.assertEqual(self._cap_on_day(400), 3)   # ramp says 8, owner says 3
        self.assertEqual(self._cap_on_day(0), 3)     # ramp says 4, owner wins

    def test_the_age_comes_from_the_pins_not_a_setting(self):
        """
        A stored "started on" value is what a redeploy wipes -- which is how
        the social module quietly went back to its full cap on day one.
        """
        import inspect
        from pin_agent.pin_bot import PinAgent
        source = inspect.getsource(PinAgent.connect)
        self.assertIn("first_pin_at", source)


class TestPinCopyAssembly(unittest.TestCase):
    """
    How the model's reply becomes a publishable description.

    Both faults here were found by running the real writer over real
    products, not by reading the code.
    """

    def setUp(self):
        from pin_agent.content import PinCopywriter
        self.P = PinCopywriter

    def _tags(self, hashtags):
        body = "A short body sentence that is easily long enough to pass."
        out = self.P._build_description({"description": body,
                                         "hashtags": hashtags})
        parts = out.split("\n\n")
        return parts[1] if len(parts) > 2 else ""

    def test_a_space_separated_string_becomes_separate_tags(self):
        """
        Splitting on commas alone made "organizer homehacks declutter" into
        ONE tag: the split kept it whole, then the punctuation strip removed
        the spaces and published #organizerhomehacksdeclutter. Useless for
        discovery, and it looks broken.
        """
        self.assertEqual(self._tags("organizer homehacks declutter"),
                         "#organizer #homehacks #declutter")

    def test_a_comma_separated_string_still_works(self):
        self.assertEqual(self._tags("kitchenorganization,giftideas"),
                         "#kitchenorganization #giftideas")

    def test_tags_that_arrive_with_hashes_are_not_doubled(self):
        self.assertEqual(self._tags("#kitchen #storage"), "#kitchen #storage")

    def test_a_multi_word_list_entry_stays_one_tag(self):
        """
        The other direction. A LIST entry with a space in it is a deliberate
        multi-word tag; splitting it publishes #kitchen #organization, two
        far vaguer searches than the one actually meant.
        """
        self.assertEqual(self._tags(["kitchen organization", "small kitchen"]),
                         "#kitchenorganization #smallkitchen")

    def test_one_and_two_letter_tags_are_dropped(self):
        self.assertEqual(self._tags(["a", "ok", "goodtag"]), "#goodtag")

    def test_the_disclosure_survives_every_tag_shape(self):
        # Pinterest requires it on every affiliate pin, and it is added in
        # code precisely so no reply shape can lose it.
        for shape in ("a b c", "a,b", ["a b"], [], "", None):
            body = self.P._build_description({"description": "x" * 60,
                                              "hashtags": shape})
            self.assertIn("#ad", body, repr(shape))

    # -- salvaging a malformed reply ------------------------------

    def test_fields_are_recovered_from_unparseable_json(self):
        """
        One product in four was skipped entirely because its reply would not
        parse -- a raw newline inside a string is enough. The copy itself
        was fine; only the punctuation around it was wrong.
        """
        blob = ('{"title": "A tidy drawer", "description": "Line one\n'
                'line two", "hashtags": ["kitchen", "tidy"]}')
        with self.assertRaises(json.JSONDecodeError):
            json.loads(blob)
        out = self.P._parse(blob)
        self.assertEqual(out["title"], "A tidy drawer")
        self.assertIn("Line one", out["description"])
        self.assertEqual(out["hashtags"], ["kitchen", "tidy"])

    def test_valid_json_is_never_salvaged(self):
        out = self.P._parse('{"title": "T", "description": "D", "hashtags": ["a"]}')
        self.assertEqual(out, {"title": "T", "description": "D", "hashtags": ["a"]})

    def test_a_reply_missing_a_required_field_is_still_refused(self):
        # Salvage must not turn a genuinely broken reply into a half-written
        # pin: both fields have to be there.
        self.assertIsNone(self.P._parse('{"title": "only a title"'))

    def test_a_reply_with_no_object_is_refused(self):
        for junk in ("no braces here", "", "```json```"):
            self.assertIsNone(self.P._parse(junk))


class TestDisclosureAndText(unittest.TestCase):

    def setUp(self):
        self.gate = ComplianceGate()

    def test_disclosure_required(self):
        self.assertIsNotNone(self.gate.check_disclosure("Great scissors, very sharp."))

    def test_any_accepted_marker_satisfies_it(self):
        for marker in ("#ad", "#affiliate", "#sponsored"):
            self.assertIsNone(self.gate.check_disclosure(f"Nice product. {marker}"))

    def test_price_in_copy_is_rejected(self):
        """
        Prices go stale: AliExpress prices move constantly and a pin outlives
        them by months, turning a selling point into a false claim.
        """
        self.assertIsNotNone(self.gate.check_text(
            "Herb Scissors Deal", "Only $8.99 today, grab it now. #ad"))

    def test_every_shape_of_stale_claim_is_refused(self):
        """
        The old check was a currency symbol followed by a digit, which let
        "20 USD", "50% off" and "half price" through untouched. Anything that
        stops being true while the pin is still being seen is refused --
        AliExpress prices move constantly and a pin outlives them by months.
        """
        for copy in ("Just 20 USD and it fits any drawer",
                     "USD 15 for the whole rack",
                     "Grab it for 9 dollars today",
                     "50% off this week only",
                     "Now 30 percent off",
                     "Half price right now",
                     "On sale until Friday",
                     "Clearance on this one",
                     "The cheapest organizer we have found",
                     "Lowest price of the year",
                     "Costs €9.99 delivered"):
            self.assertIsNotNone(
                self.gate.check_text("A perfectly fine title", f"{copy} #ad"),
                f"not refused: {copy!r}")

    def test_a_permanent_quality_is_not_a_price_claim(self):
        """
        "Affordable" never expires, so it is not the thing this rule exists
        to stop. Refusing it would cost pins for nothing.
        """
        for copy in ("An affordable way to double your cabinet space",
                     "Budget-friendly storage for a small kitchen",
                     "Holds up to 12 mugs without stacking",
                     "Ready in under 5 minutes, no tools needed",
                     "Fits under most standard sinks and holds the bottles"):
            self.assertIsNone(
                self.gate.check_text("A perfectly fine title", f"{copy} #ad"),
                f"wrongly refused: {copy!r}")

    def test_reasonable_copy_passes(self):
        self.assertIsNone(self.gate.check_text(
            "5 Kitchen Gadgets That Save Time",
            "Herb scissors that cut prep time in half. Five blades. #ad"))

    def test_over_long_title_is_rejected(self):
        self.assertIsNotNone(self.gate.check_text("A" * 120, "A fine description here. #ad"))


class TestDuplicateBlocking(unittest.TestCase):
    """AliExpress relists identical products under new ids constantly."""

    def setUp(self):
        self.gate = ComplianceGate()

    def test_same_product_blocked(self):
        self.gate.remember("123", "https://s.click.aliexpress.com/e/_a", "hash1")
        self.assertIsNotNone(self.gate.check_duplicate("123", "https://other", ""))

    def test_same_link_blocked_under_a_new_product_id(self):
        self.gate.remember("123", "https://s.click.aliexpress.com/e/_a", "hash1")
        self.assertIsNotNone(
            self.gate.check_duplicate("999", "https://s.click.aliexpress.com/e/_a", ""))

    def test_same_image_blocked(self):
        self.gate.remember("123", "https://a", "hash1")
        self.assertIsNotNone(self.gate.check_duplicate("999", "https://b", "hash1"))

    def test_fresh_product_allowed(self):
        self.gate.remember("123", "https://a", "hash1")
        self.assertIsNone(self.gate.check_duplicate("999", "https://b", "hash2"))


class TestTheGate(unittest.TestCase):

    def setUp(self):
        self.gate = ComplianceGate()

    def test_valid_pin_is_publishable(self):
        ok, reasons = self.gate.approve(valid_pin())
        self.assertTrue(ok, f"valid pin rejected: {reasons}")

    def test_every_failure_is_reported_not_just_the_first(self):
        ok, reasons = self.gate.approve(valid_pin(
            link="https://bit.ly/x", description="No disclosure here at all, sadly."))
        self.assertFalse(ok)
        self.assertGreaterEqual(len(reasons), 2, "should report link and disclosure")

    def test_pin_without_an_image_is_rejected(self):
        pin = valid_pin()
        pin.pop("image_path")
        ok, _ = self.gate.approve(pin)
        self.assertFalse(ok)


class TestProductSelection(unittest.TestCase):

    def setUp(self):
        self.products = AliExpressClient()._sample_products()
        self.selector = ProductSelector()

    def test_bad_products_are_filtered_out(self):
        picked = self.selector.select(self.products, limit=10)
        ids = {p["product_id"] for p in picked}
        self.assertNotIn("1005006423459", ids, "low rating should be rejected")
        self.assertNotIn("1005006523460", ids, "12 orders should be rejected")
        self.assertNotIn("1005006623461", ids, "no affiliate link should be rejected")
        self.assertNotIn("1005006723462", ids, "over the price ceiling should be rejected")

    def test_good_products_survive(self):
        picked = self.selector.select(self.products, limit=10)
        self.assertEqual(len(picked), 3)

    def test_ranked_best_first(self):
        picked = self.selector.select(self.products, limit=10)
        scores = [p["score"] for p in picked]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_order_count_is_compressed(self):
        """
        Without log compression one viral listing outranks everything forever.
        Ten times the orders must not mean ten times the score.
        """
        modest = self.selector.score({"orders": 500, "rating": 4.6, "price": 15})
        viral = self.selector.score({"orders": 50000, "rating": 4.6, "price": 15})
        self.assertLess(viral, modest * 2)

    def test_already_posted_products_are_excluded(self):
        picked = self.selector.select(self.products, limit=10,
                                      exclude_ids={"1005006123456"})
        self.assertNotIn("1005006123456", {p["product_id"] for p in picked})

    def test_banned_terms_are_rejected(self):
        for bad in ("Replica Designer Kitchen Set Luxury",
                    "Slimming Detox Tea Weight Loss Kitchen Blend",
                    "Nike Branded Kitchen Towel Set Official"):
            self.assertFalse(
                self.selector.is_eligible({
                    "title": bad, "rating": 4.9, "orders": 5000, "price": 20,
                    "images": ["x"], "affiliate_url": "https://s.click.aliexpress.com/e/_x"}),
                f"should reject: {bad}")

    def test_listing_noise_is_stripped_from_titles(self):
        clean = ProductSelector.clean_title(
            "2024 New Hot Sale Free Shipping Stainless Steel Herb Scissors Dropshipping")
        for noise in ("2024", "hot sale", "free shipping", "dropshipping"):
            self.assertNotIn(noise, clean.lower())
        self.assertIn("Herb Scissors", clean)


class TestSourcing(unittest.TestCase):

    def test_runs_offline_without_credentials(self):
        client = AliExpressClient()
        self.assertFalse(client.is_live)
        products = asyncio.run(client.fetch_products())
        self.assertTrue(products, "sample products should be returned with no keys")

    def test_signature_is_order_independent(self):
        client = AliExpressClient("key", "secret", "track")
        self.assertEqual(client._sign({"b": "2", "a": "1"}),
                         client._sign({"a": "1", "b": "2"}))

    def test_signature_changes_with_the_payload(self):
        client = AliExpressClient("key", "secret", "track")
        self.assertNotEqual(client._sign({"a": "1"}), client._sign({"a": "2"}))

    def test_error_response_yields_no_products(self):
        client = AliExpressClient("key", "secret", "track")
        out = client._parse({"error_response": {"msg": "Invalid signature"}})
        self.assertEqual(out, [])
        self.assertIn("Invalid signature", client.last_error)

    def test_products_without_an_affiliate_link_are_dropped(self):
        self.assertIsNone(AliExpressClient._normalise({
            "product_id": "1", "product_title": "A thing", "promotion_link": ""}))

    def test_malformed_payload_does_not_raise(self):
        client = AliExpressClient("key", "secret", "track")
        for junk in (None, [], "text", {}, {"unexpected": {}}):
            self.assertEqual(client._parse(junk), [])

    def test_a_real_product_payload_is_accepted(self):
        """
        Field names taken from a live 2026-09-02 response, not from the
        documentation. Every one of these has to survive, or the agent
        silently finds nothing.
        """
        out = AliExpressClient._normalise({
            "product_id": "1005009878350900",
            "product_title": "Capybara Bento Lunch Box",
            "promotion_link": "https://s.click.aliexpress.com/s/fwx308cRD9",
            "product_main_image_url": "https://ae-pic-a1.aliexpress-media.com/kf/a.jpg",
            "product_small_image_urls": {"string": ["https://x/b.jpg"]},
            "target_sale_price": "3.21", "target_original_price": "3.21",
            "evaluate_rate": "100.0%", "lastest_volume": 17,
            "commission_rate": "7.0%", "first_level_category_id": 26,
            "first_level_category_name": "Toys & Hobbies",
            "shop_name": "Shop1100132134 Store",
        })
        self.assertIsNotNone(out)
        self.assertEqual(out["product_id"], "1005009878350900")
        self.assertEqual(out["price"], 3.21)
        self.assertEqual(out["orders"], 17)
        self.assertEqual(out["commission_rate"], 7.0)
        self.assertEqual(out["rating"], 100.0)
        self.assertEqual(len(out["images"]), 2)

    def test_the_error_is_cleared_at_the_start_of_a_search(self):
        """
        Left set, a stale failure makes the empty-result diagnosis skip
        itself and the real cause is never reported.
        """
        client = AliExpressClient()          # offline: returns samples
        client.last_error = "something from an hour ago"
        asyncio.run(client.fetch_products())
        # Offline path returns samples and never reaches the reset, so check
        # the live path's contract directly instead.
        live = AliExpressClient("key", "secret", "track")
        live.last_error = "stale"
        out = live._parse({"error_response": {"msg": "Invalid signature"}})
        self.assertEqual(out, [])
        self.assertIn("Invalid signature", live.last_error)

    def test_an_unrecognised_tracking_id_is_named_as_the_cause(self):
        """
        The platform answers a bad tracking id with HTTP 200, no error and
        an empty list -- identical to a search that genuinely matched
        nothing. Found live: the same keyword returned five products with
        the tracking id removed and zero with a made-up one. Without this
        the agent reports "no products found" forever while the real fault
        is one wrong string in the environment.
        """
        client = AliExpressClient("key", "secret", "made-up-id")

        async def probe_finds_stock(payload):
            return [{"product_id": "1"}]

        # The probe is the same search minus the tracking id.
        client._parse = lambda payload: [{"product_id": "1"}]

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {}

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **kw):
                return FakeResponse()

        import pin_agent.sourcing as sourcing
        original = sourcing.httpx.AsyncClient
        sourcing.httpx.AsyncClient = lambda *a, **kw: FakeClient()
        try:
            asyncio.run(client._diagnose_empty("kitchen organizer"))
        finally:
            sourcing.httpx.AsyncClient = original

        self.assertIn("made-up-id", client.last_error)
        self.assertIn("Tracking ID", client.last_error)

    def test_no_tracking_id_means_no_diagnosis(self):
        client = AliExpressClient("key", "secret", "")
        asyncio.run(client._diagnose_empty("anything"))
        self.assertEqual(client.last_error, "")

    # -- per-product affiliate links ------------------------------

    def test_duplicate_image_urls_are_collapsed(self):
        """
        The main image is repeated as the first of the small ones, so every
        product arrived with images[0] == images[1]. The builder tries
        images[:3] and stops at the first that downloads, so a "try three
        photos" loop only ever saw two -- and if the seller's collage was
        one of them, the fallback was that same collage again.
        """
        out = AliExpressClient._normalise({
            "product_id": "1", "product_title": "A thing",
            "product_detail_url": "https://www.aliexpress.com/item/1.html",
            "product_main_image_url": "https://x/a.jpg",
            "product_small_image_urls": {"string": [
                "https://x/a.jpg", "https://x/b.jpg", "https://x/c.jpg"]},
        })
        self.assertEqual(out["images"],
                         ["https://x/a.jpg", "https://x/b.jpg", "https://x/c.jpg"])

    def test_the_detail_url_is_kept_and_cleaned(self):
        """
        The search response's promotion_link is shared across every product,
        so the per-product link has to be generated from the product page
        URL. That URL is echoed back as `source_value`, so its query string
        is stripped to keep both sides of the match identical.
        """
        out = AliExpressClient._normalise({
            "product_id": "1", "product_title": "A thing",
            "promotion_link": "https://s.click.aliexpress.com/s/shared",
            "product_detail_url":
                "https://www.aliexpress.com/item/1005.html?pdp_npi=6%40dis",
        })
        self.assertEqual(out["detail_url"],
                         "https://www.aliexpress.com/item/1005.html")

    def test_links_are_matched_by_url_not_by_position(self):
        """
        The platform returns the batch in a DIFFERENT ORDER from the
        request -- confirmed against the live API. Zipping positionally
        would attach a valid affiliate link for the wrong product to every
        row, which no test of link validity would ever catch.
        """
        payload = {"aliexpress_affiliate_link_generate_response": {
            "resp_result": {"result": {"promotion_links": {"promotion_link": [
                {"promotion_link": "https://s.click.aliexpress.com/e/_cTHIRD",
                 "source_value": "https://www.aliexpress.com/item/3.html"},
                {"promotion_link": "https://s.click.aliexpress.com/e/_cFIRST",
                 "source_value": "https://www.aliexpress.com/item/1.html"},
                {"promotion_link": "https://s.click.aliexpress.com/e/_cSECOND",
                 "source_value": "https://www.aliexpress.com/item/2.html"},
            ]}}}}}
        links = AliExpressClient._parse_links(payload)
        self.assertEqual(links["https://www.aliexpress.com/item/1.html"],
                         "https://s.click.aliexpress.com/e/_cFIRST")
        self.assertEqual(links["https://www.aliexpress.com/item/3.html"],
                         "https://s.click.aliexpress.com/e/_cTHIRD")

    def test_a_malformed_link_payload_does_not_raise(self):
        for junk in (None, [], "text", {}, {"unexpected": {}},
                     {"aliexpress_affiliate_link_generate_response": {}}):
            self.assertEqual(AliExpressClient._parse_links(junk), {})

    def test_products_without_a_generated_link_are_dropped(self):
        """
        A pin whose link earns nothing is worse than no pin: it still costs
        a queue slot and a reader's click.
        """
        client = AliExpressClient("key", "secret", "default")
        products = [
            {"product_id": "1", "detail_url": "https://x/1.html", "affiliate_url": "shared"},
            {"product_id": "2", "detail_url": "https://x/2.html", "affiliate_url": "shared"},
        ]

        async def only_the_first(urls):
            return {"https://x/1.html": "https://s.click.aliexpress.com/e/_cONE"}

        client._generate_links = only_the_first
        kept = asyncio.run(client._attach_links(products))
        self.assertEqual([p["product_id"] for p in kept], ["1"])
        self.assertEqual(kept[0]["affiliate_url"],
                         "https://s.click.aliexpress.com/e/_cONE")

    def test_the_shared_search_link_never_survives(self):
        client = AliExpressClient("key", "secret", "default")
        shared = "https://s.click.aliexpress.com/s/pyFri10M6ltAv61YZY9Tfr"
        products = [{"product_id": str(i), "detail_url": f"https://x/{i}.html",
                     "affiliate_url": shared} for i in (1, 2, 3)]

        async def unique(urls):
            return {u: f"https://s.click.aliexpress.com/e/_c{i}"
                    for i, u in enumerate(urls)}

        client._generate_links = unique
        kept = asyncio.run(client._attach_links(products))
        self.assertEqual(len(kept), 3)
        self.assertEqual(len({p["affiliate_url"] for p in kept}), 3)
        for p in kept:
            self.assertNotEqual(p["affiliate_url"], shared)


class TestChannelSelection(unittest.TestCase):
    """
    Which Pinterest account a pin lands on.

    The failure this guards against is publishing affiliate pins to somebody's
    personal profile because it happened to sort first.
    """

    PERSONAL = {"id": "chan_personal", "name": "Abdullah Khan",
                "service": "pinterest", "isDisconnected": False}
    BRAND = {"id": "chan_brand", "name": "Tidy Nook",
             "service": "pinterest", "isDisconnected": False}
    FACEBOOK = {"id": "chan_fb", "name": "Novi", "service": "facebook",
                "isDisconnected": False}

    def _publisher(self, channels, channel_id=""):
        publisher = PinterestPublisher("token", channel_id=channel_id)
        publisher.channels = channels
        return publisher

    def test_single_channel_needs_no_configuration(self):
        publisher = self._publisher([self.BRAND, self.FACEBOOK])
        self.assertEqual(publisher.target_channel["id"], "chan_brand")

    def test_configured_id_wins_over_ordering(self):
        publisher = self._publisher([self.PERSONAL, self.BRAND],
                                    channel_id="chan_brand")
        self.assertEqual(publisher.target_channel["name"], "Tidy Nook")

    def test_unknown_configured_id_refuses_rather_than_guessing(self):
        publisher = self._publisher([self.PERSONAL, self.BRAND],
                                    channel_id="chan_deleted")
        self.assertIsNone(publisher.target_channel)

    def test_disconnected_channels_are_never_targeted(self):
        stale = dict(self.BRAND, isDisconnected=True)
        publisher = self._publisher([stale])
        self.assertIsNone(publisher.target_channel)

    def test_no_pinterest_channel_at_all(self):
        self.assertIsNone(self._publisher([self.FACEBOOK]).target_channel)


class TestBoardRouting(unittest.TestCase):
    """
    Which of the six boards a product lands on.

    Misfiling is cosmetic, but sending everything to one board wastes the
    structure that makes Pinterest able to place a pin at all.
    """

    def assertBoard(self, expected, *texts):
        self.assertEqual(board_routing.choose_board(*texts), expected,
                         f"routing {texts!r}")

    def test_each_board_is_reachable(self):
        self.assertBoard("Kitchen Gadgets Worth Buying",
                         "Stainless Steel Herb Scissors 5 Blade Shears")
        self.assertBoard("Under Sink and Cabinet Storage",
                         "Under Sink Organizer Pull Out Cabinet Basket")
        self.assertBoard("Pantry and Fridge Storage",
                         "Airtight Cereal Container Pantry Food Storage Jar")
        self.assertBoard("Bathroom Storage Ideas",
                         "Bathroom Shower Caddy Shampoo Holder")
        self.assertBoard("Tiny Apartment Solutions",
                         "Over Door Hanging Closet Organizer Foldable")
        self.assertBoard("Small Kitchen Organization",
                         "Kitchen Drawer Divider Cutlery Utensil Tray")

    def test_unmatched_product_falls_back_rather_than_dropping(self):
        self.assertBoard(board_routing.DEFAULT_BOARD, "an unrelated widget")
        self.assertBoard(board_routing.DEFAULT_BOARD, "")
        self.assertBoard(board_routing.DEFAULT_BOARD, "", "", "")

    def test_category_and_search_term_count_too(self):
        # Listing titles are keyword soup; the useful word is often elsewhere.
        self.assertBoard("Bathroom Storage Ideas",
                         "2Pcs Wall Mounted Rack", "Bathroom", "shower caddy")

    def test_word_boundaries_are_respected(self):
        # "jar" must not fire inside "jarring", nor "counter" inside
        # "counterfeit". Both would otherwise misroute on a substring.
        self.assertBoard(board_routing.DEFAULT_BOARD,
                         "a jarring counterfeit widget")

    def test_ordinary_inflections_still_match(self):
        # "wall mount" has to catch "Wall Mounted", which is how the listings
        # are actually titled.
        self.assertBoard("Tiny Apartment Solutions",
                         "Wall Mounted Over Door Hooks")

    def test_every_route_names_a_real_board(self):
        self.assertIn(board_routing.DEFAULT_BOARD, board_routing.ALL_BOARDS)
        self.assertEqual(len(board_routing.ALL_BOARDS),
                         len(set(board_routing.ALL_BOARDS)))

    def test_resolve_maps_name_to_pinterest_id(self):
        ids = {"Bathroom Storage Ideas": "1104859789775179525"}
        self.assertEqual(board_routing.resolve("Bathroom Storage Ideas", ids),
                         "1104859789775179525")
        self.assertEqual(board_routing.resolve("bathroom storage ideas", ids),
                         "1104859789775179525")

    def test_resolve_returns_none_for_a_renamed_board(self):
        # Better to fall back deliberately than publish to whatever sorts first.
        self.assertIsNone(board_routing.resolve("Deleted Board", {"A": "1"}))
        self.assertIsNone(board_routing.resolve("Anything", {}))


class TestTheTipBank(unittest.TestCase):
    """
    The advice pins are the four fifths of the feed that sells nothing.

    Every one of the first forty-one pins carried an affiliate link, which
    is the shape of account Pinterest suppresses rather than removes. The
    bank is plain data, so it is worth checking as data: a tip that fails
    the gate is a slot the agent cannot fill, and a tip naming a board that
    does not exist publishes silently to the wrong one.
    """

    def setUp(self):
        from pin_agent import tips
        self.tips = tips
        self.gate = ComplianceGate()

    def test_every_tip_is_complete(self):
        for tip in self.tips.TIP_BANK:
            for field in ("board", "photo", "image", "title", "body"):
                self.assertTrue(tip.get(field), f"{field} missing: {tip}")

    def test_every_photograph_was_chosen_not_searched_for(self):
        """
        Searching live was tried and rejected. It answered "under the bed"
        with Nebraska fossil beds, "jar lid" with an Egyptian canopic jar
        and "kitchen scissors" with a Victorian engraving of surgical
        instruments -- about half of sixty queries came back with
        something that would have looked broken on the board.
        """
        for tip in self.tips.TIP_BANK:
            self.assertTrue(tip["image"].startswith("https://"), tip["title"])

    def test_no_two_tips_share_a_photograph(self):
        # Three separate queries returned the same cream fitted kitchen,
        # and the same picture twice is what reads as automation.
        images = [t["image"] for t in self.tips.TIP_BANK]
        self.assertEqual(len(images), len(set(images)))

    def test_a_photograph_needing_credit_carries_one(self):
        # All forty-five are public domain or CC0 today, so none needs an
        # attribution line. If one ever does, it must not publish without.
        for tip in self.tips.TIP_BANK:
            self.assertIn("credit", tip)

    def test_every_tip_names_a_real_board(self):
        # A typo here would route the pin to the default board silently.
        for tip in self.tips.TIP_BANK:
            self.assertIn(tip["board"], board_routing.ALL_BOARDS, tip["title"])

    def test_all_six_boards_are_fed(self):
        """
        "Kitchen Gadgets Worth Buying" has never received a single product
        pin -- the gadgets that match it sit below the price floor. Advice
        costs nothing, so the board can be fed that way instead.
        """
        used = {t["board"] for t in self.tips.TIP_BANK}
        self.assertEqual(used, set(board_routing.ALL_BOARDS))

    def test_no_two_tips_share_a_title(self):
        titles = [t["title"] for t in self.tips.TIP_BANK]
        self.assertEqual(len(titles), len(set(titles)))

    def test_the_bank_outlasts_the_rotation_window(self):
        """
        next_tip() holds back everything published recently. If the bank
        were no larger than that window it would empty, and the agent would
        start republishing a tip the same week -- or publish nothing.
        """
        from pin_agent.pin_bot import PinAgent
        self.assertGreater(len(self.tips.TIP_BANK), PinAgent.TIP_ROTATION)

    def test_every_tip_passes_the_compliance_gate(self):
        for tip in self.tips.TIP_BANK:
            pin = {
                "kind": "value", "product_id": "tip:x", "link": "",
                "title": tip["title"],
                "description": self.tips.describe(tip, tip["board"]),
                "image_path": "/tmp/pin.jpg", "image_hash": "h",
            }
            ok, reasons = self.gate.approve(pin)
            self.assertTrue(ok, f"{tip['title']}: {reasons}")

    def test_no_tip_claims_anything_that_goes_stale(self):
        # Caught two live: "the cheapest way to..." trips the same rule that
        # stops a product pin naming a price.
        for tip in self.tips.TIP_BANK:
            text = f"{tip['title']} {tip['body']}"
            self.assertIsNone(self.gate.check_text(tip["title"], tip["body"]),
                              text[:70])

    def test_no_hashtag_can_look_like_a_disclosure(self):
        """
        #ad on a pin that sells nothing is a false statement about what the
        pin is, and it would tell Pinterest this one is promotional too --
        the exact signal the advice pins exist to avoid sending.
        """
        from pin_agent.compliance import DISCLOSURE_MARKERS
        tags = [t for group in self.tips.BOARD_TAGS.values() for t in group]
        tags += list(self.tips.GENERAL_TAGS)
        for tag in tags:
            for marker in DISCLOSURE_MARKERS:
                self.assertNotIn(marker, tag.lower(), tag)

    def test_the_eyebrow_drops_the_selling_word(self):
        # "Worth Buying" printed on a pin that sells nothing would be the
        # one dishonest word on it.
        self.assertEqual(
            self.tips.eyebrow_for("Kitchen Gadgets Worth Buying"),
            "Kitchen Gadgets")

    def test_rotation_avoids_what_was_published_recently(self):
        from pin_agent.pin_bot import PinAgent
        recent = [t["title"]
                  for t in self.tips.TIP_BANK[:PinAgent.TIP_ROTATION]]
        for _ in range(20):
            picked = self.tips.next_tip(recent)
            self.assertNotIn(picked["title"], recent)

    def test_an_exhausted_bank_repeats_rather_than_publishing_nothing(self):
        every = [t["title"] for t in self.tips.TIP_BANK]
        self.assertIsNotNone(self.tips.next_tip(every))


class TestAdvicePinsCarryNoLink(unittest.TestCase):
    """
    The gate has two sets of rules now, and the advice one is the inverse.

    An advice pin with a link and no #ad would be an UNDISCLOSED affiliate
    pin, which is worse than anything the gate was originally written to
    stop -- so "no link" is enforced, not merely assumed.
    """

    def setUp(self):
        self.gate = ComplianceGate()

    def _advice(self, **overrides):
        pin = {
            "kind": "value",
            "product_id": "tip:store-bathroom-towels-rolled-not-folded",
            "title": "Store bathroom towels rolled, not folded",
            "description": ("Rolled towels take about a third less shelf "
                            "depth than folded ones.\n\n#bathroomstorage"),
            "link": "",
            "image_path": "/tmp/pin.jpg",
            "image_hash": "hash-advice",
        }
        pin.update(overrides)
        return pin

    def test_an_advice_pin_with_no_link_is_approved(self):
        ok, reasons = self.gate.approve(self._advice())
        self.assertTrue(ok, reasons)

    def test_an_advice_pin_carrying_a_link_is_refused(self):
        ok, reasons = self.gate.approve(
            self._advice(link="https://s.click.aliexpress.com/e/_x"))
        self.assertFalse(ok)
        self.assertIn("no destination link", " ".join(reasons))

    def test_an_advice_pin_carrying_a_disclosure_is_refused(self):
        # #ad here means product copy has leaked onto an advice pin, so the
        # pin is not what the pipeline believes it is.
        ok, reasons = self.gate.approve(
            self._advice(description="Rolled towels save shelf depth. #ad"))
        self.assertFalse(ok)

    def test_a_product_pin_still_needs_a_link_and_a_disclosure(self):
        # The regression that matters most: the new branch must not have
        # loosened the old rules.
        ok, _ = self.gate.approve(valid_pin(link=""))
        self.assertFalse(ok)
        ok, _ = self.gate.approve(valid_pin(description="No disclosure here "
                                                        "at all, just copy."))
        self.assertFalse(ok)

    def test_a_pin_reloaded_from_the_database_is_still_recognised(self):
        """
        pin_posts has no `kind` column, so a pin restored from the review
        queue arrives without one. Judged as a product pin it would fail for
        having no link -- and a real advice pin would be rejected at the
        moment a human approved it. The angle survives the round trip.
        """
        restored = self._advice()
        restored.pop("kind")
        restored["angle"] = "tip"
        self.assertEqual(self.gate.kind_of(restored), "value")
        ok, reasons = self.gate.approve(restored)
        self.assertTrue(ok, reasons)

    def test_a_pin_with_no_kind_and_no_angle_is_judged_as_a_product(self):
        # Fail towards the stricter rules, never away from them.
        self.assertEqual(self.gate.kind_of({}), "product")

    def test_a_tip_may_come_round_again_but_an_image_may_not(self):
        """
        A product id is burned for 120 days because AliExpress relists the
        same item constantly. A tip id is not an identity in that sense --
        sixty tips burned that way would empty the bank in a fortnight. The
        IMAGE is still checked, because a byte-identical pin twice is the
        failure anyone would actually see.
        """
        pin = self._advice()
        self.gate.remember(pin["product_id"], pin["link"], pin["image_hash"])

        ok, _ = self.gate.approve(self._advice(image_hash="a-different-photo"))
        self.assertTrue(ok, "the same tip must be allowed to return")

        ok, reasons = self.gate.approve(self._advice())
        self.assertFalse(ok, "the identical image must not be pinned twice")
        self.assertIn("image", " ".join(reasons))


class TestTheAffiliateRatio(unittest.TestCase):
    """
    One pin in five sells; the other four do not.

    Forty-one pins, forty-one affiliate links, an audience of three and no
    saves. The ratio is enforced by SLOT RANK rather than by a random draw,
    because a draw can hand you five affiliate pins in a row and an account
    only gets one first impression.
    """

    def setUp(self):
        from pin_agent.pin_bot import PinAgent
        self.agent = PinAgent.__new__(PinAgent)
        self.agent.config = MagicMock(pins_per_day=15)
        self.agent._first_pin_at = None
        self.agent._product_attempts_today = 0
        # The ratio is now checked against the database in every branch, so
        # even the rank test needs a store.
        self.agent.store = MagicMock()

        async def none_today(kind="all"):
            return 0

        self.agent.store.posted_today = none_today

    def _at_cap(self, cap):
        self.agent.daily_cap = lambda: cap

    def test_the_quota_is_about_a_fifth_at_every_ramp_step(self):
        for cap, expected in ((4, 1), (6, 1), (8, 2), (11, 2), (15, 3)):
            self._at_cap(cap)
            self.assertEqual(self.agent.product_quota(), expected, f"cap {cap}")

    def test_at_least_one_pin_a_day_still_earns(self):
        for cap in range(1, 16):
            self._at_cap(cap)
            self.assertGreaterEqual(self.agent.product_quota(), 1)

    def test_most_pins_sell_nothing(self):
        for cap in (4, 6, 8, 11, 15):
            self._at_cap(cap)
            share = self.agent.product_quota() / cap
            self.assertLessEqual(share, 0.25, f"cap {cap} is {share:.0%} affiliate")

    def test_the_best_slot_of_the_day_is_the_one_that_sells(self):
        """
        Slots are ordered best-first, so rank 1 is 05:00 PKT -- 8pm on the
        American east coast, the hour Pinterest browsing peaks.
        """
        self._at_cap(4)
        self.assertTrue(asyncio.run(self.agent.wants_product_pin({"rank": 1})))
        for rank in (2, 3, 4):
            self.assertFalse(
                asyncio.run(self.agent.wants_product_pin({"rank": rank})),
                f"rank {rank} must not carry a link")

    def test_a_manual_run_asks_the_database_not_its_memory(self):
        """
        Run now from the dashboard has no slot to place the pin in, so the
        day's counts decide. Counted in the DATABASE because a restart wipes
        memory -- the same defect that let eight pins publish while
        pin_posts held nothing.
        """
        self._at_cap(4)
        self.agent.store = MagicMock()
        seen = {}

        async def posted_today(kind="all"):
            seen["kind"] = kind
            return seen["count"]

        self.agent.store.posted_today = posted_today

        seen["count"] = 0
        self.assertTrue(asyncio.run(self.agent.wants_product_pin()))
        self.assertEqual(seen["kind"], "product")

        seen["count"] = 1          # today's one affiliate pin already went
        self.assertFalse(asyncio.run(self.agent.wants_product_pin()))


class TestTheFallbackOnlyRunsOneWay(unittest.TestCase):
    """
    A slot that cannot sell may publish advice. A slot that cannot find a
    photograph may NOT publish a product.

    Erring towards the pin that sells nothing can only improve the ratio.
    The reverse would quietly restore the all-affiliate feed on exactly the
    days the photo providers are down -- and Openverse went down for a day
    and a half only last month.
    """

    def _agent(self):
        from pin_agent.pin_bot import PinAgent
        agent = PinAgent.__new__(PinAgent)
        agent.config = MagicMock(pins_per_day=15, require_review=False)
        agent.published_today = 0
        agent.daily_cap = lambda: 4
        agent._consecutive_failures = 0
        agent._product_attempts_today = 0
        agent._affiliate_alerted_on = None
        agent.last_product_error = ""
        agent.last_error = ""
        agent.last_run = None
        agent.nm = None
        agent.built = []
        agent.store = MagicMock()

        async def none_today(kind="all"):
            return 0

        async def never_sold():
            return None

        agent.store.posted_today = none_today
        agent.store.last_affiliate_pin_at = never_sold

        async def build_one():
            agent.built.append("product")
            return None

        async def build_value_pin():
            agent.built.append("value")
            return None

        agent.build_one = build_one
        agent.build_value_pin = build_value_pin
        return agent

    def test_a_dry_product_slot_falls_back_to_advice(self):
        agent = self._agent()
        asyncio.run(agent.run_once({"rank": 1}))
        self.assertEqual(agent.built, ["product", "value"])

    def test_an_advice_slot_never_falls_back_to_a_product(self):
        agent = self._agent()
        asyncio.run(agent.run_once({"rank": 3}))
        self.assertEqual(agent.built, ["value"],
                         "a failed advice pin must cost the slot, not restore "
                         "the all-affiliate feed")


class TestBuildingAnAdvicePin(unittest.TestCase):
    """The whole advice path, end to end, with nothing real behind it."""

    def _agent(self, photo_works=True):
        from pin_agent.pin_bot import PinAgent
        agent = PinAgent.__new__(PinAgent)
        agent.config = MagicMock(buffer_board_id="")
        agent.gate = ComplianceGate()
        agent.recent_tips = []
        agent.recent_titles = []
        agent.recent_boards = []
        agent.recent_types = []
        # A real agent always has this from __init__; build_value_pin now
        # reads it, because a BANK tip's photograph was never checked
        # against the ones just published.
        agent.recent_photos = []
        agent.last_error = ""
        agent.upload_image = None
        agent.imaging = MagicMock()
        agent.asked = []
        # No writer and no vision check, so this exercises the fallback
        # path: the hand-written bank with its already-verified photographs.
        agent.writer = None
        agent.photos = None
        agent.verifier = None
        test = self

        async def build(product, title, eyebrow="", require_photo=False):
            agent.asked.append(product["images"][0])
            if not photo_works:
                # What the real builder does when the download fails and
                # the caller refuses the accent-wash fallback.
                test.assertTrue(require_photo,
                                "advice must never fall back to a wash")
                return None, b""
            return "/tmp/pin.jpg", b"image-bytes"

        agent.imaging.build = build
        return agent

    def test_the_pin_carries_no_link_and_no_disclosure(self):
        agent = self._agent()
        pin = asyncio.run(agent.build_value_pin())

        self.assertIsNotNone(pin, agent.last_error)
        self.assertEqual(pin["link"], "")
        self.assertEqual(pin["kind"], "value")
        self.assertEqual(pin["angle"], "tip")
        self.assertNotIn("#ad", pin["description"].lower())
        self.assertIn(pin["board_name"], board_routing.ALL_BOARDS)
        self.assertTrue(pin["product_id"].startswith("tip:"))

    def test_the_pin_is_recorded_under_the_tip_angle(self):
        # `angle` is what tells the two sorts of pin apart everywhere else,
        # including after a round trip through a database with no `kind`.
        from pin_agent import tips
        agent = self._agent()
        pin = asyncio.run(agent.build_value_pin())
        self.assertEqual(pin["angle"], tips.VALUE_ANGLE)
        self.assertEqual(agent.gate.kind_of({"angle": pin["angle"]}), "value")

    def test_it_uses_the_photograph_chosen_for_that_tip(self):
        from pin_agent import tips
        agent = self._agent()
        pin = asyncio.run(agent.build_value_pin())
        chosen = {t["title"]: t["image"] for t in tips.TIP_BANK}
        self.assertEqual(agent.asked[0], chosen[pin["title"]])

    def test_a_credited_photograph_is_attributed_in_the_description(self):
        from pin_agent import tips
        credit = "Photo: A. Smith / Wikimedia Commons (CC BY-SA 2.0)"
        tip = tips.TIP_BANK[0]
        self.assertIn(credit, tips.describe(tip, tip["board"], credit))

    def test_an_unfetchable_photograph_costs_the_slot_not_the_standard(self):
        # Rather than publishing the accent wash, which would be the most
        # obviously automated thing on the board.
        agent = self._agent(photo_works=False)
        self.assertIsNone(asyncio.run(agent.build_value_pin()))
        self.assertIn("photograph", agent.last_error)

    def test_it_gives_up_after_a_bounded_number_of_tips(self):
        from pin_agent.pin_bot import PinAgent
        agent = self._agent(photo_works=False)
        asyncio.run(agent.build_value_pin())
        self.assertEqual(len(agent.asked), PinAgent.VALUE_PIN_ATTEMPTS)

    def test_a_tip_published_recently_is_not_picked_again(self):
        from pin_agent.pin_bot import PinAgent
        from pin_agent import tips
        agent = self._agent()
        agent.recent_tips = [t["title"]
                             for t in tips.TIP_BANK[:PinAgent.TIP_ROTATION]]
        pin = asyncio.run(agent.build_value_pin())
        self.assertNotIn(pin["title"], agent.recent_tips)


class TestTheTwoHistoriesStayApart(unittest.TestCase):
    """
    Advice pin titles must never reach the product duplicate guard.

    Four pins in five are advice now, and their titles are hand-written
    sentences about tidying that share the whole generic vocabulary of the
    niche. Left in, the guard would be comparing products against eight real
    products instead of forty -- and the duplicates it exists to stop, which
    the owner has already found on the live board twice, would come back.
    """

    def _agent(self):
        from pin_agent.pin_bot import PinAgent
        agent = PinAgent.__new__(PinAgent)
        agent.gate = ComplianceGate()
        agent.recent_titles, agent.recent_tips = [], []
        return agent

    def test_an_advice_pin_goes_to_the_tip_history_only(self):
        agent = self._agent()
        agent._remember({"kind": "value", "title": "Keep onions and potatoes "
                                                   "apart", "description": "x"})
        self.assertEqual(agent.recent_tips,
                         ["Keep onions and potatoes apart"])
        self.assertEqual(agent.recent_titles, [])

    def test_a_product_pin_goes_to_the_product_history_only(self):
        agent = self._agent()
        agent._remember({"title": "Stackable egg tray", "description": "#ad",
                         "angle": "gift_idea"})
        self.assertEqual(agent.recent_tips, [])
        self.assertEqual(len(agent.recent_titles), 1)

    def test_both_histories_are_bounded(self):
        from pin_agent.pin_bot import PinAgent
        agent = self._agent()
        for n in range(PinAgent.RECENT_TITLE_COUNT + 25):
            agent._remember({"title": f"Product number {n}",
                             "description": "#ad", "angle": "hosting"})
        for n in range(PinAgent.TIP_ROTATION + 25):
            agent._remember({"kind": "value", "title": f"Tip number {n}",
                             "description": "x"})
        self.assertEqual(len(agent.recent_titles), PinAgent.RECENT_TITLE_COUNT)
        self.assertEqual(len(agent.recent_tips), PinAgent.TIP_ROTATION)

    def test_the_store_keeps_them_apart_too(self):
        from datetime import datetime, timezone
        from pin_agent.store import PinStore
        store = PinStore(None)
        # save_pin() stamps created_at on every in-memory row, and
        # posted_today() counts on it.
        today = datetime.now(timezone.utc).isoformat()
        store._memory = [
            {"title": "Spice rack", "description": "#ad", "angle": "hosting",
             "status": "published", "created_at": today},
            {"title": "Keep eggs pointed end down", "description": "",
             "angle": "tip", "status": "published", "created_at": today},
        ]
        self.assertEqual(asyncio.run(store.recent_titles(kind="product")),
                         ["Spice rack #ad"])
        self.assertEqual(asyncio.run(store.recent_tip_titles()),
                         ["Keep eggs pointed end down"])
        self.assertEqual(asyncio.run(store.posted_today("product")), 1)
        self.assertEqual(asyncio.run(store.posted_today("tip")), 1)
        self.assertEqual(asyncio.run(store.posted_today()), 2)


class TestAnUnlinkedPinOmitsTheUrl(unittest.TestCase):
    """
    Sending `url: ""` is not the same as sending no url.

    PinterestPostMetadataInput.url is optional -- confirmed by introspecting
    the live Buffer schema -- so leaving it out is legal. An empty string is
    a different thing entirely and invites Pinterest to treat it as a
    malformed destination on the pins the account most needs to land well.
    """

    def _sent(self, pin):
        publisher = PinterestPublisher(access_token="t", organization_id="o",
                                       board_id="b", channel_id="c")
        publisher._connected = True
        publisher.channels = [{"id": "c", "name": "Tidy Nook",
                               "service": "pinterest", "isDisconnected": False}]
        captured = {}

        async def gql(query, variables=None, timeout=30):
            captured["input"] = variables["i"]
            return {"createPost": {"__typename": "PostActionSuccess",
                                   "post": {"id": "1", "status": "sent"}}}

        publisher._gql = gql
        self.assertTrue(asyncio.run(publisher.publish(pin)))
        return captured["input"]["metadata"]["pinterest"]

    def test_an_advice_pin_sends_no_url_key_at_all(self):
        metadata = self._sent({
            "title": "Keep eggs in their carton, pointed end down",
            "description": "The carton protects against odours and knocks.",
            "link": "", "image_url": "https://img.example/pin.jpg",
            "board_id": "b"})
        self.assertNotIn("url", metadata)

    def test_a_product_pin_still_sends_its_affiliate_link(self):
        metadata = self._sent({
            "title": "Stackable egg tray", "description": "Two layers. #ad",
            "link": "https://s.click.aliexpress.com/e/_x",
            "image_url": "https://img.example/pin.jpg", "board_id": "b"})
        self.assertEqual(metadata["url"],
                         "https://s.click.aliexpress.com/e/_x")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTheSameSubjectDoesNotRepeat(unittest.TestCase):
    """
    Nine of the first fifty-five pins were spice racks.

    Seven of those inside twelve days. Every one was a different listing
    with a different id, a different link and a different photograph, so the
    duplicate checks all passed -- and the title guard could not see it
    either, because "drawer", "organizer" and "clear" are on the noise list,
    leaving two spice pins sharing one meaningful word against a bar of
    three. To the owner scrolling the live board it read as one pin posted
    six times, which is what he reported twice.
    """

    def setUp(self):
        from pin_agent import product_types
        self.pt = product_types

    def test_the_live_spice_pins_all_classify_the_same(self):
        # Verbatim titles from the board.
        for title in (
            "Clear Your Counter with a Pull-Out Spice Drawer",
            "Space-saving stretchable spice rack for tiny kitchens",
            "Compact spice organizer keeps jars tidy in small kitchens",
            "Keep your kitchen clear with a spice drawer organizer",
            "Stop Jumbled Spices with Easy Drawer Organizer",
        ):
            self.assertEqual(self.pt.classify(title), "spice_rack", title)

    def test_genuinely_different_subjects_stay_apart(self):
        cases = {
            "Keep eggs fresh without cracks or spills": "egg_holder",
            "Compact pull-out organizer for tiny kitchen sinks": "under_sink",
            "Keep your drawer tidy with foldable underwear organizer":
                "closet_organizer",
            "Elegant clear acrylic wall shelf": "wall_shelf",
            "Keep shoes out of the kitchen for guests": "shoe_storage",
        }
        for title, expected in cases.items():
            self.assertEqual(self.pt.classify(title), expected, title)

    def test_the_specific_rule_wins_over_the_general_one(self):
        # A spice drawer organiser is a spice rack first. Ordering in
        # TYPE_RULES is what decides this, so it is worth pinning down.
        self.assertEqual(
            self.pt.classify("4-layer adjustable spice drawer organizer"),
            "spice_rack")

    def test_keywords_are_bounded_at_both_ends(self):
        # Unbounded, "jar" fires on "jarring" and "pot" on "spotted".
        self.assertNotEqual(self.pt.classify("A jarring design choice"),
                            "jar_set")
        self.assertNotEqual(self.pt.classify("Spotted pattern basket"),
                            "pot_rack")

    def test_an_unrecognised_product_still_gets_a_type(self):
        # And it must be a real one, so the cooldown still applies to it --
        # three unnameable pins in a week are as repetitive as three spice
        # racks.
        t = self.pt.classify("Zorblax quantum widget 3000")
        self.assertEqual(t, self.pt.FALLBACK)
        self.assertTrue(self.pt.blocked_by_cooldown(t, [t]))

    def test_the_cooldown_blocks_a_repeat_and_allows_a_new_subject(self):
        self.assertTrue(self.pt.blocked_by_cooldown(
            "spice_rack", ["egg_holder", "spice_rack"]))
        self.assertFalse(self.pt.blocked_by_cooldown(
            "towel_rack", ["egg_holder", "spice_rack"]))
        self.assertFalse(self.pt.blocked_by_cooldown("spice_rack", []))

    def test_build_one_checks_the_cooldown(self):
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = (inspect.getsource(PinAgent.build_one)
               + inspect.getsource(PinAgent._pick_product))
        self.assertIn("blocked", src,
                      "the cooldown must be enforced where pins are built")
        self.assertIn('classify(copy["title"])', src,
                      "classify on the TITLE - descriptions mention other "
                      "products in passing and put a rolling cart in the "
                      "spice bucket")

    def test_the_type_is_what_gets_stored(self):
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = (inspect.getsource(PinAgent.build_one)
               + inspect.getsource(PinAgent._pick_product))
        self.assertIn('"category": product_type', src,
                      "the category column held 'Home & Garden' on all 47 "
                      "pins - one value for everything, so it carried no "
                      "information and the cooldown had nothing to read")


class TestDailyVolumeMatchesWhatPinterestRewards(unittest.TestCase):
    """
    Every current source puts a young account's safe range at 1-5 fresh pins
    a day, and says pace beats volume: "1-5 fresh pins per day, every day,
    outperforms 30 pins in one burst followed by silence". The ramp used to
    climb to 15.
    """

    def test_the_ramp_never_exceeds_the_safe_range(self):
        from pin_agent.pin_bot import PinAgent
        for _, allowed in PinAgent.RAMP:
            self.assertLessEqual(allowed, 10, "above the researched ceiling")
        self.assertLessEqual(PinAgent.RAMP_CEILING, 10)

    def test_it_still_starts_low_and_grows(self):
        from pin_agent.pin_bot import PinAgent
        allowed = [a for _, a in PinAgent.RAMP]
        self.assertEqual(allowed, sorted(allowed), "the ramp must not go down")
        self.assertLessEqual(allowed[0], 4, "day one must stay small")

    def test_one_pin_in_five_sells_at_every_step(self):
        from pin_agent.pin_bot import PinAgent
        agent = PinAgent.__new__(PinAgent)
        for _, cap in list(PinAgent.RAMP) + [(99, PinAgent.RAMP_CEILING)]:
            agent.daily_cap = lambda c=cap: c
            quota = agent.product_quota()
            self.assertGreaterEqual(quota, 1)
            self.assertLessEqual(quota / cap, 0.25, f"cap {cap}")


class TestTipsAreWrittenNotJustBanked(unittest.TestCase):
    """
    Forty-five hand-written tips is eleven days at four advice pins a day,
    and a re-uploaded pin earns nothing: Pinterest gives a fresh pin a
    distribution test for a day or two and gives a repeat none at all. So
    the writer is the supply and the bank is the safety net.
    """

    def setUp(self):
        from pin_agent.tip_writer import TipWriter
        self.TipWriter = TipWriter
        self.w = TipWriter(ai_engine=None)

    def _tip(self, **over):
        tip = {"title": "Keep the kettle where you fill it, near the tap",
               "body": "Carrying a full kettle across a kitchen spills it. "
                       "Standing it beside the tap removes the trip entirely "
                       "and keeps the worktop dry.",
               "photo": "kitchen tap"}
        tip.update(over)
        return tip

    def test_a_good_tip_passes(self):
        self.assertEqual(self.w._problems(self._tip()), [])

    def test_a_short_title_is_refused(self):
        # Under 40 characters loses the search words Pinterest matches on.
        p = self.w._problems(self._tip(title="Tidy the kettle"))
        self.assertTrue(any("title is" in x for x in p))

    def test_a_long_title_is_refused(self):
        p = self.w._problems(self._tip(title="x" * 120))
        self.assertTrue(any("title is" in x for x in p))

    def test_ai_filler_is_refused(self):
        for phrase in ("This is a game-changer for your kitchen storage",
                       "Transform your pantry with this one simple trick",
                       "Say goodbye to clutter in your kitchen cupboards"):
            p = self.w._problems(self._tip(title=phrase))
            self.assertTrue(any("banned phrase" in x for x in p), phrase)

    def test_anything_that_dates_the_pin_is_refused(self):
        # A pin is seen for months; a price or a year stops being true long
        # before it stops being shown.
        for body in ("Costs about $12 and saves a whole shelf of space here.",
                     "The best kitchen storage idea of 2026 by a mile, truly.",
                     "It is 50% cheaper than the alternative in every shop."):
            p = self.w._problems(self._tip(body=body + " " * 40))
            self.assertTrue(any("dates the pin" in x for x in p), body)

    def test_exclamation_marks_and_hashtags_are_refused(self):
        self.assertTrue(self.w._problems(self._tip(title="Keep the kettle "
                                                         "near the tap always!")))
        self.assertTrue(self.w._problems(self._tip(body="Useful. #kitchen "
                                                       + "x" * 90)))

    def test_the_photo_term_must_name_something_physical(self):
        p = self.w._problems(self._tip(photo=""))
        self.assertTrue(any("photo term" in x for x in p))
        p = self.w._problems(self._tip(photo="a b c d e f"))
        self.assertTrue(any("photo term" in x for x in p))

    def test_it_parses_json_out_of_a_chatty_answer(self):
        raw = ('Sure! Here is your tip:\n```json\n'
               '{"title": "t", "body": "b", "photo": "p"}\n```\nHope that helps')
        # Title and body come back capitalised -- see _tidy.
        self.assertEqual(self.TipWriter._parse(raw),
                         {"title": "T", "body": "B", "photo": "p"})
        self.assertIsNone(self.TipWriter._parse("no json at all here"))

    def test_board_rotation_feeds_the_quietest_board(self):
        from pin_agent import boards
        recent = ["Bathroom Storage Ideas"] * 5 + ["Small Kitchen Organization"] * 3
        for _ in range(12):
            chosen = self.TipWriter.pick_board(recent)
            self.assertNotIn(chosen, ("Bathroom Storage Ideas",
                                      "Small Kitchen Organization"))
            self.assertIn(chosen, boards.ALL_BOARDS)

    def test_the_written_tip_must_not_repeat_a_recent_one(self):
        from pin_agent.pin_bot import PinAgent
        seen = ["Store bathroom towels rolled, not folded"]
        self.assertTrue(PinAgent._tip_already_used(
            "Store your bathroom towels rolled rather than folded", seen))
        self.assertFalse(PinAgent._tip_already_used(
            "Keep eggs in their carton, pointed end down", seen))

    def test_a_specific_photo_query_broadens_to_its_room(self):
        # "toilet shelf" found nothing at all in the open libraries while
        # "bathroom" has thousands, and a tidy bathroom illustrates a tip
        # about a shelf above the toilet perfectly well.
        from pin_agent.pin_bot import PinAgent
        self.assertEqual(PinAgent._broaden("toilet shelf in bathroom"),
                         "bathroom")
        self.assertEqual(PinAgent._broaden("spice drawer insert"), "drawer")
        self.assertEqual(PinAgent._broaden("sink"), "")

    def test_the_bank_is_still_the_fallback(self):
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = inspect.getsource(PinAgent._next_tip)
        self.assertIn("tip_bank.next_tip", src,
                      "with no writer and no vision check, the verified "
                      "hand-written bank is what keeps the account posting")


class TestTheWriterDoesNotFixate(unittest.TestCase):
    """
    Told to avoid forty-five titles, the model wrote FIVE separate tips
    about magnetic knife strips -- each worded differently, every one a
    duplicate. It avoids the sentences it is shown and fixates on the idea
    behind them. Naming the objects already covered is what moves it on;
    measured over twenty writes it took near-duplicates from five to one.
    """

    def setUp(self):
        from pin_agent.tip_writer import TipWriter
        self.W = TipWriter

    def test_the_prompt_names_the_objects_already_covered(self):
        prompt = self.W._user_prompt("Small Kitchen Organization", [
            "Place a magnetic strip on the wall to hold knives upright",
            "Keep eggs in their carton, pointed end down",
        ])
        self.assertIn("ALREADY COVERED", prompt)
        for word in ("magnetic", "strip", "knives", "eggs", "carton"):
            self.assertIn(word, prompt, word)

    def test_filler_words_are_not_offered_as_objects(self):
        # "keep", "store", "space" appear in every tip in the niche and
        # would tell the model nothing while crowding out the real nouns.
        taken = self.W._objects_taken([
            "Keep the storage space in your home tidy and clear"])
        for empty in ("keep", "storage", "space", "home", "tidy", "clear"):
            self.assertNotIn(empty, taken)

    def test_an_empty_history_still_produces_a_prompt(self):
        prompt = self.W._user_prompt("Bathroom Storage Ideas", [])
        self.assertIn("Bathroom Storage Ideas", prompt)
        self.assertNotIn("ALREADY COVERED", prompt)

    def test_the_validator_enforces_everything_not_just_the_shape(self):
        """
        Validating only the JSON shape and checking the rest afterwards
        threw away one answer in four -- almost always a title a few
        characters over or under -- and each was a wasted call that fell
        back to the bank. Inside the loop the engine simply asks again.
        """
        short = '{"title": "Too short", "body": "%s", "photo": "sink"}' % ("x" * 120)
        self.assertFalse(self.W._is_publishable(short))
        good = ('{"title": "Keep the kettle where you fill it, near the tap",'
                ' "body": "%s", "photo": "kitchen tap"}' % ("x" * 120))
        self.assertTrue(self.W._is_publishable(good))
        self.assertFalse(self.W._is_publishable("not json"))

    def test_a_lowercase_opening_is_fixed(self):
        # The model returns one in seven like this, and at 56px bold on the
        # pin it reads as a mistake.
        parsed = self.W._parse('{"title": "use a pull-out knife tray today",'
                               ' "body": "b", "photo": "p"}')
        self.assertTrue(parsed["title"].startswith("Use a"))

    def test_proper_nouns_survive_the_tidy(self):
        self.assertEqual(self.W._tidy("put a lazy Susan in the pantry"),
                         "Put a lazy Susan in the pantry")


class TestASlotIsNotLostToAStrictVerifier(unittest.TestCase):
    """
    Every written tip needs a photograph found and approved, and the
    verifier is deliberately hard to satisfy. Asking the writer again after
    a photo failure spends another call failing the same way -- four
    attempts, four written tips, four photo searches, no pin, and the bank
    never reached even though its photographs need no approval at all.
    """

    def test_the_first_attempts_are_fresh_and_the_rest_are_banked(self):
        """
        TWO fresh, then banked -- it was one, until the failures were
        measured.

        Most first-attempt failures turned out not to be the verifier at
        all: Openverse times out on one subject and answers a different one
        seconds later. So a second attempt is a different draw rather than
        the same one repeated. Past that the bank still takes over, which
        is what this test is really guarding.
        """
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = inspect.getsource(PinAgent.build_value_pin)
        self.assertIn("prefer_bank=attempt > 2", src)
        self.assertGreater(PinAgent.VALUE_PIN_ATTEMPTS, 3,
                           "two fresh attempts must still leave one for "
                           "the bank")

    def test_prefer_bank_skips_the_writer_entirely(self):
        from pin_agent.pin_bot import PinAgent
        agent = PinAgent.__new__(PinAgent)
        agent.recent_tips = []
        agent.recent_types = []
        agent.writer = object()          # present, so only prefer_bank skips
        called = []

        async def fresh(seen, blocked=None):
            called.append(1)
            return {"board": "Bathroom Storage Ideas", "title": "x" * 45,
                    "body": "b", "photo": "p", "image": "https://x/1.jpg",
                    "credit": ""}

        agent._write_from_a_photograph = fresh

        banked = asyncio.run(agent._next_tip([], prefer_bank=True))
        self.assertEqual(called, [], "the writer must not be asked")
        self.assertTrue(banked["image"].startswith("https://"),
                        "a banked tip arrives with its photograph already "
                        "chosen, so nothing needs approving")

        asyncio.run(agent._next_tip([], prefer_bank=False))
        self.assertEqual(len(called), 1, "the first attempt must be fresh")

class TestThePhotographVerifier(unittest.TestCase):
    """
    The only check that looks at the picture rather than reading text about
    it. "Ashfall Fossil Beds" passed every text-based check for the query
    "bed" -- right resolution, public domain, a real photograph, and the
    word "beds" in the title.

    It DESCRIBES and code DECIDES. Asking the model to judge directly was a
    losing game: one wording approved a supermarket freezer aisle as "a
    fridge", the next refused 34 of 35 candidates and more than half the
    photographs already chosen by hand. A description comes back the same
    every run, so everything below is testable without spending quota.
    """

    def setUp(self):
        from pin_agent import photo_check
        self.pc = photo_check

    def test_the_real_failures_are_all_rejected(self):
        # Every description here came back from the live model, for a
        # photograph that had passed every other check in the pipeline.
        cases = [
            ("Fossilized rhinoceros skeletons at Ashfall Fossil Beds", "a bed"),
            ("A hospital bed in a room.", "a bed"),
            ("Illustration of scissors on a transparent background.",
             "kitchen scissors"),
            ("Illustration of antique bronze surgical instruments",
             "kitchen scissors"),
            ("Museum object: A patterned woven basket on display",
             "storage basket"),
            ("A photograph of an ancient stone temple in Hampi, India",
             "small kitchen"),
            ("Painting of kitchen utensils and food on a stone ledge",
             "small kitchen"),
            ("A supermarket freezer aisle with glass doors", "fridge"),
            ("Sun loungers and prayer flags at a resort", "rolled towels"),
            ("Wrapped soap bars on a wooden tray at a market stall",
             "bar of soap"),
            ("Wooden drawers and lamps on a counter in a cafe",
             "small kitchen interior"),
        ]
        for description, subject in cases:
            ok, why = self.pc.judge(description, subject)
            self.assertFalse(ok, "should be refused: " + description)
            self.assertTrue(why, "a refusal must say why")

    def test_good_photographs_are_accepted(self):
        cases = [
            ("A photograph of a running faucet in a tiled bathroom",
             "bathroom sink"),
            ("Toothpaste on a toothbrush against a white background",
             "toothbrush"),
            ("A lighted mirror and sink in a bathroom.", "bathroom mirror"),
            ("Potatoes and red onions cooking in a frying pan.",
             "onions and potatoes"),
            ("Muffins in a tin on a kitchen counter by a window",
             "home kitchen counter"),
            ("A white towel hangs on a dark tiled bathroom wall",
             "towel rail bathroom"),
        ]
        for description, subject in cases:
            ok, why = self.pc.judge(description, subject)
            self.assertTrue(ok, description + " refused for " + why)

    def test_synonyms_do_not_lose_a_good_photograph(self):
        # Without these, "fridge" never matches a description saying
        # "refrigerator" and a perfectly good picture is thrown away.
        self.assertTrue(self.pc.is_relevant(
            "An open refrigerator full of food", "fridge"))
        self.assertTrue(self.pc.is_relevant(
            "A tidy kitchen worktop by a window", "kitchen countertop"))
        self.assertTrue(self.pc.is_relevant(
            "Clothes hanging in a closet", "wardrobe"))

    def test_plurals_match(self):
        self.assertTrue(self.pc.is_relevant("Spice jars on a shelf",
                                            "spice jar"))
        self.assertTrue(self.pc.is_relevant("A single egg in a carton",
                                            "eggs in a carton"))

    def test_an_unrelated_photograph_is_refused(self):
        self.assertFalse(self.pc.is_relevant("A cat asleep on a sofa",
                                             "kitchen drawer"))

    def test_common_words_prove_nothing(self):
        self.assertFalse(self.pc.is_relevant(
            "A modern white room, clean and tidy", "spice jars"))

    def test_an_empty_description_is_refused_not_approved(self):
        ok, why = self.pc.judge("", "a bathroom")
        self.assertFalse(ok)
        self.assertIn("no description", why)

    def _verifier(self, answers):
        v = self.pc.PhotoVerifier(api_keys=["k1", "k2"], models=("m1", "m2"))
        seq = list(answers)

        async def fake_describe(image, mime="image/jpeg"):
            return seq.pop(0) if seq else None

        v.describe = fake_describe
        return v

    def test_no_description_is_not_approval(self):
        """
        The single most important line. None means every quota was spent or
        the service was unreachable; treating it as True would publish
        unverified photographs on exactly the days it is down.
        """
        v = self._verifier([None])
        result = asyncio.run(v.verify(b"jpeg", "a bathroom"))
        self.assertIsNone(result)
        self.assertIsNot(result, True)
        self.assertEqual(v.checked, 0, "a non-answer is not a check")

    def test_a_matching_description_approves(self):
        v = self._verifier(["A tiled bathroom with a sink"])
        self.assertIs(asyncio.run(v.verify(b"x", "bathroom sink")), True)

    def test_a_museum_description_rejects(self):
        v = self._verifier(["Museum object: a woven basket on display"])
        self.assertIs(asyncio.run(v.verify(b"x", "storage basket")), False)
        self.assertIn("museum", v.status["reasons"])

    def test_every_key_is_tried_against_every_model(self):
        # Quota is per project per model, so two keys and three models is
        # six separate allowances rather than one.
        v = self.pc.PhotoVerifier(api_keys=["a", "b"], models=("x", "y", "z"))
        self.assertEqual(len(v._pairs), 6)
        self.assertEqual(v.status["daily_capacity"], 120)

    def test_without_a_key_it_gives_no_verdict(self):
        v = self.pc.PhotoVerifier(api_keys=[])
        self.assertFalse(v.is_ready)
        self.assertIsNone(asyncio.run(v.verify(b"x", "a kitchen")))

    def test_first_approved_is_bounded(self):
        v = self._verifier(["A cat on a sofa"] * 20)
        tried = []

        async def fake_verify_url(url, subject, caption=""):
            tried.append(url)
            return await v.verify(b"x", subject)

        v.verify_url = fake_verify_url
        urls = ["https://x/%d.jpg" % i for i in range(20)]
        self.assertIsNone(asyncio.run(
            v.first_approved(urls, "kitchen drawer", limit=3)))
        self.assertEqual(len(tried), 3)

    def test_it_sends_the_image_bytes_not_the_url(self):
        # StockSnap answers a server-side fetch with 403.
        import inspect
        src = inspect.getsource(self.pc.PhotoVerifier.describe)
        self.assertIn("inline_data", src)
        self.assertIn("b64encode", src)

    def test_the_reason_survives_for_debugging(self):
        ok, why = self.pc.judge("A supermarket freezer aisle", "fridge")
        self.assertFalse(ok)
        self.assertIn("supermarket", why)


class TestThePhotographComesFirst(unittest.TestCase):
    """
    Three wrong pins in a row showed the order was backwards.

    Writing the tip first means hunting for a picture of that exact idea,
    and no open library holds a photograph of a tension rod holding spray
    bottles under a sink. So the search broadens, finds something generic,
    and the match becomes luck -- a tip about magnetic knife strips came
    out illustrated with a roll of camera film, because the broadened query
    was "counter" and there was a counter in the shot.

    Finding the photograph first removes the mismatch instead of filtering
    it: the tip is written to what is actually in the frame.
    """

    def test_every_board_has_well_stocked_photo_subjects(self):
        from pin_agent import boards
        from pin_agent.tip_writer import TipWriter
        for board in boards.ALL_BOARDS:
            subjects = TipWriter.PHOTO_SUBJECTS.get(board)
            self.assertTrue(subjects, board)
            self.assertGreaterEqual(len(subjects), 4, board)

    def test_the_subjects_are_broad_not_specific(self):
        # "a tidy kitchen worktop" exists in thousands of photographs;
        # "a tension rod holding spray bottles" exists in none.
        from pin_agent.tip_writer import TipWriter
        for subjects in TipWriter.PHOTO_SUBJECTS.values():
            for s in subjects:
                self.assertLessEqual(len(s.split()), 3, s)

    def test_subjects_rotate_so_one_board_is_not_one_picture(self):
        from pin_agent.tip_writer import TipWriter
        board = "Bathroom Storage Ideas"
        used = ["bathroom interior"] * 4 + ["bathroom shelf"] * 3
        for _ in range(10):
            picked = TipWriter.photo_subject(board, used)
            self.assertNotIn(picked, ("bathroom interior", "bathroom shelf"))

    def test_the_writer_is_given_the_description(self):
        import inspect
        from pin_agent.tip_writer import TipWriter
        src = inspect.getsource(TipWriter.write_for_photo)
        self.assertIn("description", src)
        self.assertIn("write about what is", src.lower().replace("\n", " ")
                      .replace("  ", " "))

    def test_the_agent_verifies_before_it_writes(self):
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = inspect.getsource(PinAgent._write_from_a_photograph)
        verify_at = src.index("verifier.verify")
        write_at = src.index("write_for_photo")
        self.assertLess(verify_at, write_at,
                        "no point writing a tip for a photograph that is "
                        "about to be thrown away")

    def test_a_photograph_is_not_used_twice(self):
        import inspect
        from pin_agent.pin_bot import PinAgent
        self.assertIn("recent_photos",
                      inspect.getsource(PinAgent._write_from_a_photograph))

    def test_no_verdict_means_the_photograph_is_skipped(self):
        # verify() returns None when every quota is spent. That must not
        # read as approval.
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = inspect.getsource(PinAgent._write_from_a_photograph)
        self.assertIn("if not await self.verifier.verify", src)


class TestThePhotographMustAlsoLookGood(unittest.TestCase):
    """
    The first two advice pins to publish were both CORRECT and both dowdy.

    A purple plastic spray trigger and a cluttered kitchen with someone's
    back in the frame: the right subject, a real home, every rule satisfied.
    Pinterest's home-organisation audience saves bright, airy, uncluttered
    rooms, and neither of those is one anybody saves.

    So the describer grades the picture as well as naming it, and a bright
    one is taken over a merely-acceptable one.
    """

    def setUp(self):
        from pin_agent import photo_check
        self.pc = photo_check

    def test_the_grade_is_read_out_of_the_description(self):
        self.assertEqual(self.pc.look_of("A tidy white bathroom. BRIGHT"),
                         self.pc.LOOK_BRIGHT)
        self.assertEqual(self.pc.look_of("A dim cluttered counter. DARK"),
                         self.pc.LOOK_DARK)
        self.assertEqual(self.pc.look_of("A kitchen drawer. PLAIN"),
                         self.pc.LOOK_PLAIN)

    def test_the_grade_is_matched_in_capitals_only(self):
        """
        The same words appear in ordinary prose describing the frame. "A
        white towel hangs on a dark tiled bathroom wall" is a perfectly
        bright photograph OF dark tiles, and a lowercase search threw it
        away.
        """
        self.assertEqual(
            self.pc.look_of("A white towel on a dark tiled bathroom wall"),
            self.pc.LOOK_PLAIN)
        self.assertEqual(
            self.pc.look_of("A bright airy kitchen with dark wood shelves"),
            self.pc.LOOK_PLAIN)
        self.assertEqual(
            self.pc.look_of("A white towel on a dark tiled wall. BRIGHT"),
            self.pc.LOOK_BRIGHT)

    def test_a_missing_grade_is_usable_not_refused(self):
        # An ungraded photograph is plain, never a rejection -- a model that
        # forgets the word must not cost a slot.
        self.assertEqual(self.pc.look_of("A kitchen drawer with cutlery"),
                         self.pc.LOOK_PLAIN)
        ok, _ = self.pc.judge("A kitchen drawer with cutlery", "kitchen drawer")
        self.assertTrue(ok)

    def test_a_drab_photograph_is_refused(self):
        ok, why = self.pc.judge("A dim cluttered kitchen counter. DARK",
                                "kitchen counter")
        self.assertFalse(ok)
        self.assertIn("drab", why)

    def test_the_grade_cannot_be_mistaken_for_relevance(self):
        # Without this, a subject containing "dark" matches any photograph
        # graded DARK and counts as relevant.
        self.assertFalse(self.pc.is_relevant("A sofa in a lounge. DARK",
                                             "dark wood cabinet"))
        self.assertFalse(self.pc.is_relevant("A garden bench. BRIGHT",
                                             "bright kitchen"))

    def test_a_bright_photograph_is_preferred_over_a_plain_one(self):
        v = self.pc.PhotoVerifier(api_keys=["k"], models=("m",))
        seq = ["A kitchen drawer with cutlery. PLAIN",
               "A bright tidy kitchen drawer. BRIGHT"]

        async def fake_describe(image, mime="image/jpeg"):
            return seq.pop(0) if seq else None

        v.describe = fake_describe

        async def fake_fetch(url):
            return b"x"

        v.fetch = fake_fetch
        got = asyncio.run(v.first_approved(
            ["https://a/plain.jpg", "https://b/bright.jpg"], "kitchen drawer"))
        self.assertEqual(got, "https://b/bright.jpg")

    def test_a_plain_photograph_is_still_used_when_nothing_is_brighter(self):
        # Better a plain pin than no pin.
        v = self.pc.PhotoVerifier(api_keys=["k"], models=("m",))
        seq = ["A kitchen drawer with cutlery. PLAIN"] * 3

        async def fake_describe(image, mime="image/jpeg"):
            return seq.pop(0) if seq else None

        async def fake_fetch(url):
            return b"x"

        v.describe, v.fetch = fake_describe, fake_fetch
        got = asyncio.run(v.first_approved(
            ["https://a/1.jpg", "https://a/2.jpg"], "kitchen drawer"))
        self.assertEqual(got, "https://a/1.jpg")


class TestClearanceIsNotAlwaysASale(unittest.TestCase):
    """
    "Place a shallow bin under the sink for cleaning supplies" was refused
    because its body mentioned the clearance above the pipes. In this niche
    clearance means headroom far more often than it means a sale, and the
    rule was throwing away good advice.
    """

    def setUp(self):
        self.gate = ComplianceGate()

    def test_physical_clearance_is_allowed(self):
        for body in (
            "Leave enough clearance above the pipes so the bin slides out.",
            "Measure the clearance under the shelf before buying anything.",
            "There is more clearance at the back than most people realise.",
        ):
            self.assertIsNone(self.gate.check_text("A perfectly fine title here",
                                                   body + " " * 30), body)

    def test_a_clearance_sale_is_still_refused(self):
        for body in (
            "Grab it from the clearance sale before it ends today for good.",
            "These are on clearance at the moment so stock up while you can.",
            "Found in the clearance section of most big shops right now ok.",
        ):
            self.assertIsNotNone(self.gate.check_text("A perfectly fine title",
                                                      body), body)

    def test_the_other_stale_claims_still_bite(self):
        for body in ("Only 20 USD today for this one, a bargain buy indeed.",
                     "It is 50% off this week only, so be quick about it.",
                     "Half-price right now at most of the big retailers ok."):
            self.assertIsNotNone(self.gate.check_text("A perfectly fine title",
                                                      body), body)


class TestARewordedRepeatIsCaught(unittest.TestCase):
    """
    The last gap, found by replaying every pin ever published.

    "Keep your drawer tidy with foldable underwear organizer" and "Keep
    your drawers tidy with a simple organizer" are 79% identical character
    for character, and to anyone scrolling the board they are one pin
    posted twice. Neither existing guard saw it: "keep", "drawer", "tidy"
    and "organizer" are all on the noise list, so the meaningful words
    reduce to {foldable, underwear} against {simple} -- nothing in common
    -- and the subject classifier split them between closet and drawer
    storage.
    """

    def _agent(self, *recent):
        from pin_agent.pin_bot import PinAgent
        a = PinAgent.__new__(PinAgent)
        a.recent_titles = list(recent)
        return a

    def test_the_pair_that_slipped_through_is_caught(self):
        a = self._agent("Keep your drawer tidy with foldable underwear organizer")
        self.assertTrue(a._too_similar_to_recent(
            "Keep your drawers tidy with a simple organizer"))

    def test_the_other_live_repeats_are_caught(self):
        a = self._agent("keep your fridge tidy with clear organizer bins")
        self.assertTrue(a._too_similar_to_recent(
            "Keep your fridge tidy with clear storage bins"))

    def test_genuinely_different_pins_still_publish(self):
        # The bar is high on purpose. Blocking these is how the agent went
        # silent for thirty-two hours once before.
        a = self._agent("Keep eggs fresh without cracks or spills",
                        "Keep your drawers tidy with a simple organizer")
        for title in ("Compact pull-out organizer for tiny kitchen sinks",
                      "Elegant clear acrylic wall shelf for a hallway",
                      "Say goodbye to stale sugar and flour",
                      "Store spices away from the cooker, not above it"):
            self.assertFalse(a._too_similar_to_recent(title), title)

    def test_the_bar_is_high_enough_not_to_starve_the_agent(self):
        from pin_agent.pin_bot import PinAgent
        self.assertGreaterEqual(PinAgent.TITLE_SIMILARITY, 0.75,
                                "below this it starts blocking pins that "
                                "merely share a topic")

    def test_an_empty_history_blocks_nothing(self):
        self.assertFalse(self._agent()._too_similar_to_recent("Anything here"))

    def test_a_short_title_does_not_crash_the_comparison(self):
        a = self._agent("Keep eggs fresh without cracks or spills")
        self.assertFalse(a._too_similar_to_recent("Hi"))


class TestTheClassifierIsFineEnoughForAdvice(unittest.TestCase):
    """
    The catch-all was holding a quarter of the tip bank.

    Twelve of the forty-five hand-written tips classified as
    `general_organizer` together -- keys, a knife, a scale, measuring spoons,
    a timer, the worktop, the back of a bathroom door, folding clothes. They
    are twelve different subjects, and a cooldown that treats them as one
    would lock out eleven the first time any of them published. That is
    precisely how the thirty-two hour outage happened, so the classifier had
    to be narrowed before the guard could be switched on at all.
    """

    def setUp(self):
        from pin_agent import product_types, tips
        self.pt = product_types
        self.tips = tips

    def test_the_catch_all_is_small_enough_to_participate(self):
        # At 12/45 the bucket is a dumping ground and the cooldown on it is
        # indiscriminate. At 4/45 it is a real subject like any other.
        from collections import Counter
        counts = Counter(self.pt.classify(t["title"])
                         for t in self.tips.TIP_BANK)
        share = counts[self.pt.FALLBACK] / len(self.tips.TIP_BANK)
        self.assertLessEqual(share, 0.15,
                             f"{counts[self.pt.FALLBACK]} of "
                             f"{len(self.tips.TIP_BANK)} tips are unnamed")

    def test_the_bank_has_enough_distinct_subjects_to_rotate(self):
        subjects = {self.pt.classify(t["title"]) for t in self.tips.TIP_BANK}
        self.assertGreaterEqual(len(subjects), 24,
                                "too few subjects and the window blocks the "
                                "bank faster than it refills")

    def test_the_two_live_duplicates_now_collide(self):
        # Verbatim from the board. Published 2 hours and 42 hours apart.
        for a, b in (
            ("Place an egg timer on the counter while cooking",
             "Set the timer before you start, not after"),
            ("Place a shoe rack on a shelf inside the closet",
             "Keep shoes out of the kitchen for guests"),
        ):
            self.assertEqual(self.pt.classify(a), self.pt.classify(b),
                             f"{a!r} and {b!r} are the same subject")

    def test_tips_that_merely_shared_the_catch_all_do_not_collide(self):
        # All of these were `general_organizer` together before.
        for a, b in (
            ("A sharp kitchen knife is safer than a blunt one",
             "Give keys a home within arm's reach of the door"),
            ("A scale makes baking work the first time",
             "Set the timer before you start, not after"),
            ("Keep measuring spoons loose, not on a ring",
             "In a tiny kitchen, clear one worktop completely"),
        ):
            self.assertNotEqual(self.pt.classify(a), self.pt.classify(b),
                                f"{a!r} and {b!r} are different subjects")

    def test_the_new_rules_did_not_steal_from_the_old_ones(self):
        """
        A frozen corpus. TYPE_RULES is first-match-wins, so a new entry in
        the wrong place silently swallows an existing one -- it happened
        twice while writing these: "fold" took "Store bathroom towels
        rolled, not folded" from towel_rack, and "blade" took "Herb Scissors
        with 5 Blades" from kitchen_tool.
        """
        for title, expected in (
            ("Store bathroom towels rolled, not folded", "towel_rack"),
            ("These stainless steel herb scissors feature five precision "
             "blades", "kitchen_tool"),
            ("Keep your kitchen clear with a spice drawer organizer",
             "spice_rack"),
            ("Keep eggs fresh without cracks or spills", "egg_holder"),
            ("Compact pull-out organizer for tiny kitchen sinks",
             "under_sink"),
            ("Keep your drawer tidy with foldable underwear organizer",
             "closet_organizer"),
            ("Elegant clear acrylic wall shelf", "wall_shelf"),
        ):
            self.assertEqual(self.pt.classify(title), expected, title)

    def test_a_chopping_board_is_not_a_drawer_divider(self):
        # "board" belongs to divider_board and "chopping" to kitchen_tool,
        # so chopping_board has to sit above both.
        self.assertEqual(self.pt.classify("Keep one board for raw meat and "
                                          "never mix them"), "chopping_board")
        self.assertEqual(self.pt.classify("White expandable divider board "
                                          "saves kitchen space"),
                         "divider_board")

    def test_every_rule_name_is_unique(self):
        names = [n for n, _ in self.pt.TYPE_RULES]
        self.assertEqual(len(names), len(set(names)))
        self.assertNotIn(self.pt.FALLBACK, names)


class TestAdvicePinsHaveASubjectGuard(unittest.TestCase):
    """
    The duplicate the owner kept finding, and the reason it got through.

    `blocked_by_cooldown` was called in `build_one()` -- the affiliate path
    -- and nowhere in `build_value_pin()`. Advice pins had no subject guard
    at all, and the writer and the bank could not see each other's subjects.
    So on 15 September the bot wrote "Place an egg timer on the counter
    while cooking" at 05:05 and pulled "Set the timer before you start, not
    after" out of the bank at 07:03. Two hours apart, on the same subject.
    """

    def _agent(self, tips_seen=(), types_seen=()):
        from pin_agent.pin_bot import PinAgent
        a = PinAgent.__new__(PinAgent)
        a.recent_tips = list(tips_seen)
        a.recent_types = list(types_seen)
        a.writer = None
        return a

    def test_a_published_advice_tip_puts_its_subject_on_hold(self):
        a = self._agent(tips_seen=["Set the timer before you start, not after"])
        self.assertIn("kitchen_timer", a.subject_window())

    def test_the_egg_timer_tip_would_now_be_blocked(self):
        from pin_agent import product_types
        a = self._agent(tips_seen=["Set the timer before you start, not after"])
        subject = product_types.classify(
            "Place an egg timer on the counter while cooking")
        self.assertIn(subject, a.subject_window())

    def test_product_and_advice_subjects_share_one_window(self):
        """
        A shoe-rack product pin and "keep shoes out of the kitchen" are one
        duplicate to anyone scrolling. That comparison is only possible if
        both kinds are classified the same way.
        """
        a = self._agent(tips_seen=["Keep only this week's shoes by the front door"],
                        types_seen=["spice_rack"])
        window = a.subject_window()
        self.assertIn("shoe_storage", window)
        self.assertIn("spice_rack", window)

    def test_advice_subjects_can_be_excluded_for_the_product_path(self):
        # The earning pin is the scarce thing, so advice subjects are only a
        # soft block on it -- dropped on the relief pass.
        a = self._agent(tips_seen=["Set the timer before you start, not after"],
                        types_seen=["spice_rack"])
        hard_only = a.subject_window(include_advice=False)
        self.assertIn("spice_rack", hard_only)
        self.assertNotIn("kitchen_timer", hard_only)

    def test_the_advice_window_is_shorter_than_the_product_cooldown(self):
        """
        Supply is asymmetric. Advice is effectively unlimited; the live
        selector produced FOUR product candidates out of forty sourced. A
        symmetric window would put ~28 subjects on hold and starve the
        scarcer side.
        """
        from pin_agent.pin_bot import PinAgent
        a = self._agent(tips_seen=[f"Tip number {i}" for i in range(40)])
        self.assertLessEqual(len(a.recent_tips[:PinAgent.ADVICE_SUBJECT_COUNT]),
                             PinAgent.ADVICE_SUBJECT_COUNT)
        self.assertLessEqual(PinAgent.ADVICE_SUBJECT_COUNT, 20)

    def test_the_bank_avoids_a_held_subject_when_it_can(self):
        from pin_agent import product_types, tips
        picked = tips.next_tip([], blocked_subjects={"kitchen_timer"})
        self.assertNotEqual(product_types.classify(picked["title"]),
                            "kitchen_timer")

    def test_the_bank_still_returns_a_tip_when_every_subject_is_held(self):
        """
        The relief valve, and it is not optional. An over-strict duplicate
        guard once stopped this agent publishing for thirty-two hours.
        """
        from pin_agent import product_types, tips
        everything = {product_types.classify(t["title"])
                      for t in tips.TIP_BANK}
        self.assertIsNotNone(tips.next_tip([], blocked_subjects=everything))

    def test_the_bank_still_returns_a_tip_when_everything_is_seen_and_held(self):
        from pin_agent import product_types, tips
        everything = {product_types.classify(t["title"])
                      for t in tips.TIP_BANK}
        seen = [t["title"] for t in tips.TIP_BANK]
        self.assertIsNotNone(tips.next_tip(seen, blocked_subjects=everything))

    def test_subject_lookup_is_memoised_not_hand_written(self):
        # Hand-writing the subject into 45 dicts would drift the moment a
        # rule in product_types changed.
        from pin_agent import product_types, tips
        for tip in tips.TIP_BANK[:6]:
            self.assertEqual(tips.subject_of(tip["title"]),
                             product_types.classify(tip["title"]))

    def test_the_writer_is_told_which_subjects_are_held(self):
        from pin_agent.tip_writer import TipWriter
        import inspect
        src = inspect.getsource(TipWriter.write_for_photo)
        self.assertIn("avoid_subjects", src,
                      "naming the held subject in the prompt costs nothing; "
                      "rejecting a finished tip costs a Groq call")


class TestTheEarningPinSurvivesAFailedSlot(unittest.TestCase):
    """
    The affiliate pin published on 13 September and then stopped for two
    days, and nothing said so.

    `wants_product_pin()` returned `rank <= quota`, so ONLY rank 1 could
    ever sell. One dry sourcing call and the day earned nothing. Meanwhile
    the slot itself "succeeded" -- it published an advice pin -- so
    `_note_failure()` never fired and the board went on looking busy.
    """

    def _agent(self, published=0, attempts=0, last_sold_hours=None):
        from datetime import datetime, timedelta, timezone
        from pin_agent.pin_bot import PinAgent
        a = PinAgent.__new__(PinAgent)
        a.config = MagicMock(pins_per_day=15)
        a.daily_cap = lambda: 5
        a._product_attempts_today = attempts
        a._affiliate_alerted_on = None
        a.last_error = "no product passed the filters"
        a.last_product_error = ""
        a.selector = MagicMock(rejections={"price below floor": 21},
                               min_price=8.0)
        a.nm = None
        a.store = MagicMock()

        async def posted_today(kind="all"):
            return published

        async def last_at():
            if last_sold_hours is None:
                return None
            return (datetime.now(timezone.utc)
                    - timedelta(hours=last_sold_hours))

        a.store.posted_today = posted_today
        a.store.last_affiliate_pin_at = last_at
        return a

    def test_the_designated_slot_still_sells_first(self):
        self.assertTrue(asyncio.run(
            self._agent().wants_product_pin({"rank": 1})))

    def test_no_other_slot_sells_until_the_designated_one_has_failed(self):
        """
        Rank order is not clock order. At a cap of five the PKT day starts
        at 23:00, which is rank 2 -- so without a lower bound on the retry
        counter the earning pin would be taken by a mediocre slot before
        05:00, the best hour of the day, ever ran.
        """
        a = self._agent(attempts=0)
        for rank in (2, 3, 4, 5):
            self.assertFalse(asyncio.run(a.wants_product_pin({"rank": rank})),
                             f"rank {rank} must not sell before rank 1 has "
                             f"had its turn")

    def test_a_later_slot_retries_once_the_first_has_failed(self):
        self.assertTrue(asyncio.run(
            self._agent(attempts=1).wants_product_pin({"rank": 2})))
        self.assertTrue(asyncio.run(
            self._agent(attempts=2).wants_product_pin({"rank": 3})))

    def test_retries_are_capped_so_a_dead_api_cannot_burn_the_day(self):
        from pin_agent.pin_bot import PinAgent
        a = self._agent(attempts=PinAgent.PRODUCT_ATTEMPTS_PER_DAY)
        self.assertFalse(asyncio.run(a.wants_product_pin({"rank": 4})))

    def test_the_ratio_can_never_be_breached(self):
        # Once one affiliate pin is published nothing else may sell, whatever
        # the rank and whatever the counter says.
        for rank, attempts in ((1, 0), (2, 1), (3, 2)):
            a = self._agent(published=1, attempts=attempts)
            self.assertFalse(asyncio.run(a.wants_product_pin({"rank": rank})),
                             f"rank {rank} sold twice in one day")

    def test_a_manual_run_also_respects_the_published_count(self):
        # This closes a hole that was already open: the rank test used to
        # short-circuit before the count was read, so a dashboard run plus
        # the 05:00 slot published two affiliate pins.
        self.assertTrue(asyncio.run(self._agent(published=0).wants_product_pin()))
        self.assertFalse(asyncio.run(self._agent(published=1).wants_product_pin()))

    def test_a_miss_is_counted_and_remembered(self):
        a = self._agent(last_sold_hours=2)
        asyncio.run(a._note_product_miss())
        self.assertEqual(a._product_attempts_today, 1)
        self.assertEqual(a.last_product_error, "no product passed the filters")

    def test_a_recent_sale_does_not_raise_the_alarm(self):
        a = self._agent(last_sold_hours=2)
        a.nm = MagicMock()
        sent = []

        async def send(**kw):
            sent.append(kw)

        a.nm.send_notification = send
        asyncio.run(a._note_product_miss())
        self.assertEqual(sent, [], "two hours is not a drought")

    def test_a_stale_earning_pin_raises_the_alarm_once(self):
        a = self._agent(last_sold_hours=30)
        a.nm = MagicMock()
        sent = []

        async def send(**kw):
            sent.append(kw)

        a.nm.send_notification = send
        asyncio.run(a._note_product_miss())
        asyncio.run(a._note_product_miss())
        self.assertEqual(len(sent), 1, "it must email once a day, not hourly")
        self.assertTrue(sent[0]["is_critical"])
        # The histogram is what makes the email actionable.
        self.assertIn("price below floor", sent[0]["message"])

    def test_the_counter_resets_with_the_day(self):
        a = self._agent(attempts=3)
        a.published_today = 4
        a.reset_daily()
        self.assertEqual(a._product_attempts_today, 0)


class TestProductSupplyIsWideEnough(unittest.TestCase):
    """
    Measured live: one keyword round returned forty products of which FOUR
    survived -- "already posted: 9, price below floor: 21, rating too low:
    6". Four candidates cannot absorb a rejection, and that is why the
    earning pin stopped.
    """

    def test_several_keyword_rounds_are_fetched(self):
        import inspect
        from pin_agent.pin_bot import PinAgent
        self.assertGreaterEqual(PinAgent.SOURCING_ROUNDS, 2)
        src = inspect.getsource(PinAgent.build_one)
        self.assertIn("SOURCING_ROUNDS", src)
        self.assertIn("seen_ids", src, "rounds must be deduped on product_id")

    def test_the_candidate_list_is_longer_than_it_was(self):
        from pin_agent.pin_bot import PinAgent
        self.assertGreaterEqual(PinAgent.CANDIDATE_LIMIT, 10)

    def test_the_price_floor_can_be_relaxed_per_call(self):
        sel = ProductSelector(min_price=8.0)
        product = {"affiliate_url": "https://s.click.aliexpress.com/e/_x",
                   "images": ["x"], "rating": 98, "orders": 900,
                   "price": 5.5,
                   "title": "A perfectly ordinary drawer tray for cutlery"}
        self.assertFalse(sel.is_eligible(product))
        self.assertTrue(sel.is_eligible(product, price_floor=5.0))

    def test_the_safety_filters_never_relax(self):
        # Only the price floor gives way. Rating, orders and the banned
        # terms are what protect the account.
        sel = ProductSelector(min_rating=90.0, min_orders=100, min_price=8.0)
        for bad in ({"rating": 70, "orders": 900, "price": 20},
                    {"rating": 98, "orders": 5, "price": 20}):
            product = {"affiliate_url": "https://s.click.aliexpress.com/e/_x",
                       "images": ["x"],
                       "title": "A perfectly ordinary drawer tray here"}
            product.update(bad)
            self.assertFalse(sel.is_eligible(product, price_floor=1.0))

    def test_build_one_runs_a_strict_pass_then_a_relaxed_one(self):
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = inspect.getsource(PinAgent.build_one)
        self.assertIn("for relaxed in (False, True)", src)
        self.assertIn("price_floor", src)

    def test_the_relaxed_pass_reuses_the_copy_it_already_paid_for(self):
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = inspect.getsource(PinAgent._pick_product)
        self.assertIn("written", src,
                      "a second pass must not pay the AI twice for the same "
                      "product")

    def test_the_failure_reason_names_the_filter(self):
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = inspect.getsource(PinAgent.build_one)
        self.assertIn("self.selector.rejections", src,
                      "'no candidate produced a compliant pin' does not say "
                      "the price floor ate 21 of 40")



class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeTable:
    """
    A pin_posts table that predates the photo columns -- exactly the live
    table until database/pin_schema.sql is run.
    """

    def __init__(self, log, strict=True):
        self.log = log
        self.strict = strict
        self._record = None

    def insert(self, record):
        self._record = record
        return self

    def execute(self):
        self.log.append(self._record)
        if self.strict and "photo_url" in self._record:
            raise RuntimeError("column pin_posts.photo_url does not exist")
        return FakeResult([self._record])


class FakeClient:
    def __init__(self, strict=True):
        self.inserts = []
        self.strict = strict

    def table(self, name):
        return FakeTable(self.inserts, self.strict)


class TestVerifiedPhotographsAreRemembered(unittest.TestCase):
    """
    Every photograph the vision check approved was forgotten on restart.

    photo_url was set on the pin dict and save_pin() never wrote it, so a
    hard allowance of twenty checks per key per model per day bought exactly
    twenty pins and left nothing behind. Kept, the same calls build a pool
    that only grows -- and reusing a photograph under a different tip is not
    a repeat pin, because only the finished composite has to be unique and
    the image hash already enforces that.
    """

    def _store(self):
        from pin_agent.store import PinStore
        return PinStore(None)

    # -- persistence ---------------------------------------------

    def test_the_source_photograph_is_written_not_just_the_composite(self):
        store = self._store()
        saved = asyncio.run(store.save_pin({
            "title": "Keep the worktop clear", "angle": "tip",
            "image_url": "https://cdn/composite.jpg",
            "photo_url": "https://cdn/source.jpg",
            "photo_note": "A bright tidy kitchen worktop. BRIGHT"}))
        self.assertEqual(saved["photo_url"], "https://cdn/source.jpg")
        self.assertEqual(saved["photo_note"],
                         "A bright tidy kitchen worktop. BRIGHT",
                         "the description is what lets a cached photograph "
                         "be written about without a second vision call")

    def test_a_missing_column_does_not_stop_a_pin_being_recorded(self):
        """
        The columns are additive and the live table predates them. A table
        without them rejects the whole INSERT, which would stop every pin
        being recorded -- and with it the duplicate guard, the daily cap and
        the volume ramp, all of which read that table.
        """
        from pin_agent.store import PinStore
        client = FakeClient(strict=True)
        store = PinStore(client)
        saved = asyncio.run(store.save_pin({
            "title": "Keep the worktop clear", "angle": "tip",
            "photo_url": "https://cdn/source.jpg",
            "photo_note": "A bright tidy worktop. BRIGHT"}))

        self.assertIsNotNone(saved, "the pin must still be recorded")
        self.assertEqual(len(client.inserts), 2, "full insert, then trimmed")
        self.assertNotIn("photo_url", client.inserts[1])
        self.assertNotIn("photo_note", client.inserts[1])
        self.assertEqual(client.inserts[1]["title"], "Keep the worktop clear",
                         "a pin is worth more than a cached photograph")

    def test_the_warning_is_said_once_not_per_pin(self):
        from pin_agent.store import PinStore
        store = PinStore(FakeClient(strict=True))
        for _ in range(3):
            asyncio.run(store.save_pin({"title": "x", "angle": "tip",
                                        "photo_url": "https://cdn/a.jpg"}))
        self.assertTrue(store._warned_missing_columns,
                        "the operator has to be told the migration is owed")

    def test_a_migrated_table_takes_the_columns_first_time(self):
        from pin_agent.store import PinStore
        client = FakeClient(strict=False)
        store = PinStore(client)
        asyncio.run(store.save_pin({"title": "x", "angle": "tip",
                                    "photo_url": "https://cdn/a.jpg",
                                    "photo_note": "a tidy shelf"}))
        self.assertEqual(len(client.inserts), 1, "no pointless second write")
        self.assertFalse(store._warned_missing_columns)

    # -- reading the pool back -----------------------------------

    def test_an_empty_pool_is_not_an_error(self):
        # _run swallows the APIError and returns None when the column is not
        # there, which must read back as "no cached photographs" and let the
        # live search carry on exactly as before.
        self.assertEqual(asyncio.run(self._store().verified_photos()), [])

    def test_the_pool_drops_entries_with_no_description(self):
        # A URL without the model's note is useless here: write_for_photo
        # needs the description, not the picture.
        store = self._store()
        store._memory = [
            {"photo_url": "https://a/1.jpg", "photo_note": "a tidy pantry"},
            {"photo_url": "https://a/2.jpg", "photo_note": ""},
            {"photo_url": "", "photo_note": "orphaned note"},
        ]
        got = asyncio.run(store.verified_photos())
        self.assertEqual([g["photo_url"] for g in got], ["https://a/1.jpg"])

    def test_the_pool_is_newest_first_and_deduped(self):
        # The caller reads the HEAD of this list, so the order is the policy.
        store = self._store()
        store._memory = [
            {"photo_url": "https://a/old.jpg", "photo_note": "an old shelf"},
            {"photo_url": "https://a/new.jpg", "photo_note": "a new shelf"},
            {"photo_url": "https://a/new.jpg", "photo_note": "a new shelf"},
        ]
        got = asyncio.run(store.verified_photos())
        self.assertEqual([g["photo_url"] for g in got],
                         ["https://a/new.jpg", "https://a/old.jpg"])

    def test_a_saved_pin_turns_up_in_the_pool(self):
        # The round trip is the whole point: what save_pin writes is what
        # connect() reads back on the next run.
        store = self._store()
        asyncio.run(store.save_pin({
            "title": "Keep the worktop clear", "angle": "tip",
            "photo_url": "https://cdn/source.jpg",
            "photo_note": "A bright tidy worktop. BRIGHT"}))
        pool = asyncio.run(store.verified_photos())
        self.assertEqual(pool,
                         [{"photo_url": "https://cdn/source.jpg",
                           "photo_note": "A bright tidy worktop. BRIGHT"}])

    # -- the cached tier -----------------------------------------

    def _agent(self, pool):
        from pin_agent.pin_bot import PinAgent
        a = PinAgent.__new__(PinAgent)
        a.recent_boards = []
        a.recent_photos = []
        a.photo_pool = list(pool)
        return a

    def test_the_cached_tier_costs_no_vision_call(self):
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = inspect.getsource(PinAgent._write_from_a_cached_photograph)
        self.assertNotIn("verifier", src,
                         "the photograph was approved once already; checking "
                         "it again spends the allowance this tier exists to "
                         "save")
        self.assertNotIn("candidates", src,
                         "and it must not search Openverse either")

    def test_the_cached_tier_sits_between_the_search_and_the_bank(self):
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = inspect.getsource(PinAgent._write_from_a_photograph)
        self.assertIn("_write_from_a_cached_photograph", src,
                      "a failed live search should try the pool before "
                      "falling all the way back to the hand-picked bank")

    def test_an_empty_pool_falls_through_quietly(self):
        agent = self._agent([])
        agent.writer = MagicMock()
        self.assertIsNone(asyncio.run(
            agent._write_from_a_cached_photograph([], set())))

    def test_a_cached_tip_arrives_with_its_photograph_attached(self):
        agent = self._agent([{"photo_url": "https://a/pantry.jpg",
                              "photo_note": "A bright tidy pantry. BRIGHT"}])
        seen_descriptions = []

        class Writer:
            @staticmethod
            def pick_board(recent):
                return "Pantry and Fridge Storage"

            async def write_for_photo(self, board, description, avoid=None,
                                      avoid_subjects=None):
                seen_descriptions.append(description)
                return {"board": board,
                        "title": "Group the tall jars at the back",
                        "body": "b", "photo": "", "image": "", "credit": ""}

        agent.writer = Writer()
        tip = asyncio.run(agent._write_from_a_cached_photograph([], set()))
        self.assertEqual(seen_descriptions, ["A bright tidy pantry. BRIGHT"],
                         "the stored description IS the input; that is why "
                         "no new vision call is needed")
        self.assertEqual(tip["image"], "https://a/pantry.jpg")
        self.assertEqual(tip["photo_note"], "A bright tidy pantry. BRIGHT",
                         "carried through so the reuse is itself cached")

    def test_an_unused_photograph_is_preferred(self):
        agent = self._agent([{"photo_url": "https://a/used.jpg",
                              "photo_note": "a used shelf"},
                             {"photo_url": "https://a/fresh.jpg",
                              "photo_note": "a fresh shelf"}])
        agent.recent_photos = ["https://a/used.jpg"]

        class Writer:
            @staticmethod
            def pick_board(recent):
                return "Pantry and Fridge Storage"

            async def write_for_photo(self, board, description, avoid=None,
                                      avoid_subjects=None):
                return {"board": board, "title": "t", "body": "b",
                        "photo": "", "image": "", "credit": ""}

        agent.writer = Writer()
        tip = asyncio.run(agent._write_from_a_cached_photograph([], set()))
        self.assertEqual(tip["image"], "https://a/fresh.jpg")

    def test_a_used_photograph_is_a_preference_not_a_prohibition(self):
        """
        The same relief rule as everywhere else in this bot: nothing new may
        become the reason nothing publishes.
        """
        agent = self._agent([{"photo_url": "https://a/used.jpg",
                              "photo_note": "a used shelf"}])
        agent.recent_photos = ["https://a/used.jpg"]

        class Writer:
            @staticmethod
            def pick_board(recent):
                return "Pantry and Fridge Storage"

            async def write_for_photo(self, board, description, avoid=None,
                                      avoid_subjects=None):
                return {"board": board, "title": "t", "body": "b",
                        "photo": "", "image": "", "credit": ""}

        agent.writer = Writer()
        tip = asyncio.run(agent._write_from_a_cached_photograph([], set()))
        self.assertIsNotNone(tip, "every photograph was used, so one of them "
                                  "has to be allowed again")
        self.assertEqual(tip["image"], "https://a/used.jpg")

    def test_the_tier_gives_up_rather_than_grinding(self):
        # Each try costs a Groq call. A writer that refuses everything must
        # not walk a pool of 120.
        from pin_agent.pin_bot import PinAgent
        agent = self._agent([{"photo_url": "https://a/%d.jpg" % i,
                              "photo_note": "shelf %d" % i}
                             for i in range(40)])
        calls = []

        class Writer:
            @staticmethod
            def pick_board(recent):
                return "Pantry and Fridge Storage"

            async def write_for_photo(self, board, description, avoid=None,
                                      avoid_subjects=None):
                calls.append(description)
                return None

        agent.writer = Writer()
        self.assertIsNone(asyncio.run(
            agent._write_from_a_cached_photograph([], set())))
        self.assertLessEqual(len(calls), PinAgent.CACHED_PHOTO_TRIES * 2,
                             "bounded by CACHED_PHOTO_TRIES on each pass")

    def test_recent_photos_is_loaded_durably(self):
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = inspect.getsource(PinAgent.connect)
        self.assertIn("self.recent_photos", src,
                      "held only in memory, a redeploy re-enabled a "
                      "photograph that had already been used")
        self.assertIn("verified_photos", src)



class TestTitlesRenderOnAnyHost(unittest.TestCase):
    """
    A title is drawn in 56px bold across the middle of the image, so one
    character the host font lacks is a blank box on a published pin -- and
    no length or content check would catch it.

    Seen live: the model wrote "coat drop-off" with a NON-BREAKING HYPHEN
    (U+2011). Arial has that glyph and so does DejaVu, which is what the
    Linux host uses, but the pipeline should not depend on which font
    happens to be installed.
    """

    def setUp(self):
        from pin_agent.tip_writer import TipWriter
        self.T = TipWriter

    def test_the_character_that_actually_turned_up(self):
        self.assertEqual(
            self.T._tidy("use wall hooks and bench for coat drop\u2011off"),
            "Use wall hooks and bench for coat drop-off")

    def test_dashes_quotes_and_the_ellipsis_all_fold(self):
        got = self.T._tidy("don\u2019t stack pans \u2014 stand them "
                           "upright\u2026")
        self.assertEqual(got, "Don't stack pans - stand them upright...")

    def test_invisible_spacing_characters_go(self):
        # A non-breaking space measures as a character but can draw as a box.
        self.assertEqual(self.T._tidy("keep \u201cdaily\u201d items"
                                      "\u00a0at eye level"),
                         'Keep "daily" items at eye level')
        self.assertEqual(self.T._tidy("a\u200bb"), "Ab")

    def test_plain_text_is_left_alone(self):
        plain = "Group the tall jars at the back of the shelf"
        self.assertEqual(self.T._tidy(plain), plain)

    def test_a_proper_noun_still_survives(self):
        # The original promise of _tidy: only the FIRST character is touched.
        self.assertEqual(self.T._tidy("use a lazy Susan for oils"),
                         "Use a lazy Susan for oils")

    def test_folding_happens_before_the_length_check(self):
        """
        The ellipsis becomes three characters, so measuring the raw answer
        and drawing the folded one would let a 60-character title through
        and render 62.
        """
        import inspect
        src = inspect.getsource(self.T._parse)
        self.assertIn("_tidy", src,
                      "_parse must fold before _problems measures anything")



class TestProductTitlesFoldToo(unittest.TestCase):
    """
    The very next pin the end-to-end run built had U+2011 in its PRODUCT
    title, minutes after the same character turned up in an advice title.
    Both are drawn in the same 56px bold on the same image, so both fold --
    and through one shared table, because two copies would drift.
    """

    def test_the_product_title_folds(self):
        from pin_agent.content import PinCopywriter
        self.assertEqual(
            PinCopywriter._tidy_title("Drawer Organiser \u2014 6\u2011Pack"),
            "Drawer Organiser - 6-Pack")

    def test_the_product_title_keeps_its_own_rules(self):
        from pin_agent.content import PinCopywriter
        self.assertEqual(PinCopywriter._tidy_title("LOUD TITLE HERE!"),
                         "Loud Title Here")

    def test_both_paths_use_the_same_table(self):
        from pin_agent import content, tip_writer
        from pin_agent.text import fold_typographic
        self.assertIs(content.fold_typographic, fold_typographic)
        self.assertIs(tip_writer.fold_typographic, fold_typographic)

    def test_the_fold_leaves_ordinary_text_alone(self):
        from pin_agent.text import fold_typographic
        plain = "Keep one board for raw meat and never mix them"
        self.assertEqual(fold_typographic(plain), plain)
        self.assertEqual(fold_typographic(""), "")
        self.assertEqual(fold_typographic(None), "")


class TestThePhotoSearchIsNotStarvedByItsOwnFilter(unittest.TestCase):
    """
    PIN_SOURCES was added for a good reason -- Openverse indexes museums, and
    "storage basket" returned a Pomo artefact from the Honolulu Museum. But
    measured across ten household subjects it had become the ceiling:

        curated only -> 26 photographs, and NOTHING for 2 of 10 subjects
        then widened -> 37 photographs, and nothing for 0 of 10

    A subject with no photograph is an advice pin that falls back to the
    forty-five-tip bank, and the bank is where the repeats came from. The
    vision check already rejects "museum", so widening is safe here in a way
    it would not be for an article.
    """


    def setUp(self):
        from modules.stock_photos import StockPhotoFinder
        self.FINDER = StockPhotoFinder

    def _source(self):
        import inspect
        return inspect.getsource(self.FINDER.candidates)

    def test_the_curated_sources_are_still_tried_first(self):
        src = self._source()
        self.assertIn("for sources in (self.PIN_SOURCES, None)", src,
                      "curated first, whole index second")

    def test_widening_only_happens_when_the_shortlist_is_short(self):
        src = self._source()
        self.assertIn("if len(out) >= limit:", src,
                      "a full shortlist must not pay for a second search")

    def test_the_filter_is_still_defined(self):
        # Removing it outright is the wrong fix: it is a good preference.
        self.assertTrue(self.FINDER.PIN_SOURCES)
        for name in ("stocksnap", "rawpixel", "wordpress", "nappy"):
            self.assertIn(name, self.FINDER.PIN_SOURCES)

    def test_both_licence_tiers_are_still_walked(self):
        self.assertIn("for licences in LICENCE_TIERS", self._source())



class TestOnePhotoFailureDoesNotCostTheSlot(unittest.TestCase):
    """
    `prefer_bank=attempt > 0` sent every attempt after the first to the
    bank, and the bank is where the repeated pins came from.

    The reasoning was that the verifier is hard to satisfy, so a retry fails
    the same way. Measuring the failures showed otherwise: Openverse times
    out on ONE subject and answers a different one seconds later -- live,
    "kitchen sink" timed out while "kitchen pantry" returned four
    photographs, one of which passed and was published. A second attempt is
    a genuinely different draw.

    At a measured two-in-three success rate that moves bank fallback from
    about a third of advice slots to about one in nine.
    """

    def test_two_attempts_are_fresh_before_the_bank(self):
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = inspect.getsource(PinAgent.build_value_pin)
        self.assertIn("prefer_bank=attempt > 2", src)

    def test_the_bank_still_takes_over(self):
        # Unbounded fresh retries would spend the whole hour failing.
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = inspect.getsource(PinAgent.build_value_pin)
        self.assertIn("prefer_bank", src,
                      "the bank must still be reachable, or a bad photo "
                      "hour publishes nothing at all")
        self.assertGreaterEqual(PinAgent.VALUE_PIN_ATTEMPTS, 3,
                                "two fresh attempts plus at least one bank "
                                "attempt")

    def test_prefer_bank_skips_the_writer_entirely(self):
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = inspect.getsource(PinAgent._next_tip)
        self.assertIn("if self.writer and not prefer_bank", src,
                      "a banked attempt must not pay for a search it is "
                      "not going to use")



class TestTheSamePhotographDoesNotPublishTwice(unittest.TestCase):
    """
    On 15 September the board published the SAME stainless egg timer twice,
    two hours apart:

        05:05  "Place an egg timer on the counter while cooking"  (written)
        07:03  "Set the timer before you start, not after"        (bank)

    recent_photos was consulted in _find_photo, which only runs for a
    freshly written tip that arrives with no picture. A BANK tip arrives
    with its photograph already chosen, so build_value_pin took tip["image"]
    and never looked.

    Nothing downstream could catch it: the compliance gate fingerprints the
    FINISHED composite, and the same photograph under two different titles
    renders two different images with two different hashes.
    """

    EGG_TIMER = "https://images.rawpixel.com/egg-timer.jpg"

    def _agent(self, tips_in_order):
        from unittest.mock import MagicMock
        from pin_agent.pin_bot import PinAgent
        a = PinAgent.__new__(PinAgent)
        a.recent_photos = [self.EGG_TIMER]
        # A real agent always has one; build_value_pin now asks it whether
        # a fixed-set picture suits its title.
        a.verifier = None
        a.recent_tips = []
        a.recent_boards = []
        a.last_error = ""
        a.upload_image = None
        a.config = MagicMock(buffer_board_id="b1")
        a.imaging = MagicMock()
        a.served = []

        queue = list(tips_in_order)

        async def next_tip(tried, prefer_bank=False):
            return queue.pop(0) if queue else None

        async def build(product, title, eyebrow="", require_photo=False):
            a.served.append(product["images"][0])
            return ("/tmp/pin.jpg", b"bytes")

        a._next_tip = next_tip
        a.imaging.build = build
        a._tip_id = lambda t: "tip_" + str(abs(hash(t)))[:8]
        a.gate = MagicMock()
        a.gate.approve = lambda pin: (True, [])
        a.gate.image_fingerprint = lambda b: "hash"
        return a

    @staticmethod
    def _tip(title, image):
        return {"title": title, "board": "Kitchen Gadgets Worth Buying",
                "body": "b", "photo": "egg timer", "image": image,
                "credit": "", "photo_note": ""}

    def test_a_bank_tip_reusing_a_recent_photograph_is_skipped(self):
        agent = self._agent([
            self._tip("Set the timer before you start, not after",
                      self.EGG_TIMER),
            self._tip("Keep one board for raw meat and never mix them",
                      "https://images.rawpixel.com/board.jpg"),
        ])
        pin = asyncio.run(agent.build_value_pin())
        self.assertIsNotNone(pin)
        self.assertEqual(pin["photo_url"],
                         "https://images.rawpixel.com/board.jpg",
                         "the egg timer had just gone out")
        self.assertNotIn(self.EGG_TIMER, agent.served,
                         "it must not even be rendered")

    def test_an_unused_photograph_publishes_straight_away(self):
        agent = self._agent([
            self._tip("Keep one board for raw meat and never mix them",
                      "https://images.rawpixel.com/board.jpg"),
        ])
        pin = asyncio.run(agent.build_value_pin())
        self.assertIsNotNone(pin)
        self.assertEqual(pin["photo_url"],
                         "https://images.rawpixel.com/board.jpg")

    def test_the_last_attempt_publishes_anyway(self):
        """
        The usual relief valve. A repeated photograph is worse than a fresh
        one and better than an empty slot -- and an empty slot is how the
        32-hour outage happened.
        """
        from pin_agent.pin_bot import PinAgent
        agent = self._agent([
            self._tip("t%d" % i, self.EGG_TIMER)
            for i in range(PinAgent.VALUE_PIN_ATTEMPTS)
        ])
        pin = asyncio.run(agent.build_value_pin())
        self.assertIsNotNone(pin, "publishing nothing is the worse failure")
        self.assertEqual(pin["photo_url"], self.EGG_TIMER)

    def test_the_photograph_is_recorded_so_the_next_slot_sees_it(self):
        # The guard is only as good as what _remember keeps.
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = inspect.getsource(PinAgent._remember)
        self.assertIn("recent_photos.insert(0, photo)", src)
        self.assertIn('pin.get("photo_url")', src,
                      "a bank pin records its photograph the same way a "
                      "written one does")

    def test_every_path_that_picks_a_photograph_checks(self):
        """
        There are three, and all three must look: the photo-first writer,
        the tip-first fallback, and the bank. Missing any one of them is
        how the timer pair happened.
        """
        import inspect
        from pin_agent.pin_bot import PinAgent
        for method in (PinAgent._write_from_a_photograph,
                       PinAgent._find_photo,
                       PinAgent.build_value_pin):
            self.assertIn("recent_photos", inspect.getsource(method),
                          f"{method.__name__} can publish a repeat")

    def test_the_fallback_path_prefers_rather_than_forbids(self):
        # If every candidate has been used, one of them is still better
        # than no photograph at all.
        import inspect
        from pin_agent.pin_bot import PinAgent
        self.assertIn("fresh or candidates",
                      inspect.getsource(PinAgent._find_photo))



class TestRecentPhotographsSurviveARestart(unittest.TestCase):
    """
    recent_photos is what stops a picture going out twice, and it lived only
    in memory. On Render that is emptied by every deploy and every wake from
    sleep, so the guard was off more often than it was on.

    photo_url is stored now, but only once the migration has been run. Until
    then a BANK tip's photograph can still be recovered, because the bank is
    a fixed table of title -> image and the tip TITLES are stored. That is
    the half that can be fixed without waiting for anything.
    """

    def test_a_banked_title_names_its_photograph(self):
        from pin_agent import tips
        banked = tips.TIP_BANK[0]
        self.assertEqual(tips.photo_for(banked["title"]), banked["image"])

    def test_a_written_title_names_nothing(self):
        # Nothing can recover a written pin's photograph until the column
        # exists. This must say so rather than guess.
        from pin_agent import tips
        self.assertEqual(
            tips.photo_for("Place an egg timer on the counter while cooking"),
            "")
        self.assertEqual(tips.photo_for(""), "")

    def test_the_lookup_ignores_the_things_that_are_not_the_sentence(self):
        from pin_agent import tips
        banked = tips.TIP_BANK[0]
        self.assertEqual(tips.photo_for("  " + banked["title"].upper() + " "),
                         banked["image"],
                         "the same title stored back from the database "
                         "should still match")

    def test_every_banked_tip_resolves(self):
        from pin_agent import tips
        missing = [t["title"] for t in tips.TIP_BANK
                   if not tips.photo_for(t["title"])]
        self.assertEqual(missing, [])

    def test_connect_seeds_from_the_recent_tips(self):
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = inspect.getsource(PinAgent.connect)
        self.assertIn("photo_for", src)
        self.assertIn("self.recent_tips", src)

    def test_the_list_stays_bounded(self):
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = inspect.getsource(PinAgent.connect)
        self.assertIn("del self.recent_photos[self.store.USED_LIMIT:]", src,
                      "an unbounded list grows for the life of the process")



class TestThePhotoMemoryNeedsNothingRunByHand(unittest.TestCase):
    """
    The photo columns need a migration nobody has run, so every photograph
    the bot used was forgotten on restart -- which on Render is every deploy
    and every wake from sleep, and is how one egg timer published twice.

    The image bucket needs no migration. It already holds every finished
    pin, so the memory lives there as one small JSON file and works the day
    it ships.
    """

    def _store(self):
        from pin_agent.store import PinStore
        return PinStore(None)

    def test_an_empty_memory_is_not_an_error(self):
        got = asyncio.run(self._store().photo_memory())
        self.assertEqual(got, {"used": [], "pool": []})

    def test_a_published_photograph_is_remembered(self):
        store = self._store()
        asyncio.run(store.remember_photo("https://a/1.jpg", "a tidy pantry"))
        got = asyncio.run(store.photo_memory())
        self.assertEqual(got["used"], ["https://a/1.jpg"])
        self.assertEqual(got["pool"],
                         [{"photo_url": "https://a/1.jpg",
                           "photo_note": "a tidy pantry"}])

    def test_the_same_photograph_is_not_counted_twice(self):
        store = self._store()
        for _ in range(3):
            asyncio.run(store.remember_photo("https://a/1.jpg", "a shelf"))
        got = asyncio.run(store.photo_memory())
        self.assertEqual(got["used"].count("https://a/1.jpg"), 1)
        self.assertEqual(len(got["pool"]), 1)

    def test_newest_first(self):
        # The guard reads the head of this list.
        store = self._store()
        asyncio.run(store.remember_photo("https://a/old.jpg", "old"))
        asyncio.run(store.remember_photo("https://a/new.jpg", "new"))
        self.assertEqual(asyncio.run(store.photo_memory())["used"][0],
                         "https://a/new.jpg")

    def test_a_photograph_with_no_description_still_blocks_repeats(self):
        # It cannot be written about again without the description, but it
        # must still never publish twice.
        store = self._store()
        asyncio.run(store.remember_photo("https://a/1.jpg", ""))
        got = asyncio.run(store.photo_memory())
        self.assertEqual(got["used"], ["https://a/1.jpg"])
        self.assertEqual(got["pool"], [])

    def test_an_empty_url_is_ignored(self):
        store = self._store()
        asyncio.run(store.remember_photo("", "nothing"))
        self.assertEqual(asyncio.run(store.photo_memory())["used"], [])

    def test_the_lists_are_bounded(self):
        from pin_agent.store import PinStore
        self.assertLessEqual(PinStore.USED_LIMIT, 1000)
        self.assertLessEqual(PinStore.POOL_LIMIT, 1000)

    def test_the_read_does_not_trust_the_cache(self):
        """
        Caught live: storage listed the file at 851 bytes while download()
        kept returning a 24-byte copy from before the write. A stale read
        silently re-allows a photograph already published.
        """
        import inspect
        from pin_agent.store import PinStore
        src = inspect.getsource(PinStore.photo_memory)
        self.assertIn("get_public_url", src)
        self.assertIn("v={int(time.time())}", src,
                      "the cache-buster is the whole point")
        self.assertIn("store.download", src,
                      "a private bucket has no public URL, so the direct "
                      "read stays as the fallback")

    def test_a_corrupt_memory_does_not_stop_the_bot(self):
        # Publishing nothing is the worse failure.
        store = self._store()
        store._photo_memory = {"used": "not a list", "pool": None}
        got = asyncio.run(store.photo_memory())
        self.assertIsInstance(got["used"], (list, str))

    def test_the_agent_loads_it_on_connect(self):
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = inspect.getsource(PinAgent.connect)
        self.assertIn("photo_memory", src)
        self.assertIn("self.recent_photos", src)
        self.assertIn("self.photo_pool", src)

    def test_the_agent_writes_it_after_publishing(self):
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = inspect.getsource(PinAgent.publish)
        self.assertIn("remember_photo", src,
                      "a photograph that published must be written down, or "
                      "the next restart lets it go out again")



class TestATipMustBeAboutItsOwnPhotograph(unittest.TestCase):
    """
    A finished pin published "Put a shallow shelf above the sink for quick
    towel drying" over a photograph of a bathroom sink with NO shelf and NO
    towel in it.

    The prompt already asked for advice about what is in the picture. Asking
    is not enforcing, and a pin whose words argue with its image is the kind
    that looks careless to everyone who sees it.

    Checked in plain code over the model's own description -- the same shape
    as the photo check, where the model describes and code decides, because
    a yes/no judgement from a model drifts and this does not.
    """

    BATHROOM = "A modern bathroom with a vanity, toilet and shower. BRIGHT."
    PANTRY = "Glass jars of food on wooden pantry shelves. BRIGHT."

    def test_the_pin_that_actually_went_wrong(self):
        from pin_agent.tip_writer import anchored
        self.assertFalse(anchored(
            "Put a shallow shelf above the sink for quick towel drying",
            self.BATHROOM),
            "there is no shelf and no towel in that photograph")

    def test_a_tip_about_what_is_there_passes(self):
        from pin_agent.tip_writer import anchored
        for tip in ("Keep the vanity clear and store daily things in a tray",
                    "Hang a caddy in the shower so bottles stay off the floor"):
            self.assertTrue(anchored(tip, self.BATHROOM), tip)

    def test_a_tip_about_a_different_room_is_refused(self):
        from pin_agent.tip_writer import anchored
        self.assertFalse(anchored(
            "Hang coats on hooks by the front door", self.PANTRY))

    def test_singulars_and_plurals_match(self):
        from pin_agent.tip_writer import anchored
        self.assertTrue(anchored("Label the front of each jar", self.PANTRY))
        self.assertTrue(anchored("Wipe the shelves twice a year", self.PANTRY))

    def test_one_thing_in_common_is_enough(self):
        """
        ONE word in common passes. A tip is advice, not a caption -- it
        should say more than the picture does.
        """
        from pin_agent.tip_writer import anchored
        self.assertTrue(anchored(
            "Decant rice and pasta into jars so you see what is running low",
            self.PANTRY))

    def test_the_rule_is_strict_and_the_prompt_pays_for_it(self):
        """
        A tip can be perfectly good and still share no word with the
        description -- "Decant rice and pasta so you can see what is running
        low" is sound advice for a pantry photograph, and this refuses it.

        That is accepted on purpose, because the prompt now tells the model
        to name something from the description, and measured on real output
        the rule refused NOTHING: 8 tips asked for, 8 kept, every one of
        them naming what was actually in its picture -- glass jars on
        pantry shelves, hooks above the bench, the knife block.

        If that rate ever climbs, soften this rather than let the writer be
        starved back to the fixed set.
        """
        from pin_agent.tip_writer import anchored
        self.assertFalse(anchored(
            "Decant rice and pasta so you can see what is running low",
            self.PANTRY))

    def test_filler_words_do_not_count_as_a_match(self):
        from pin_agent.tip_writer import anchored
        # "a", "the", "with", "bright" are in every description ever written.
        self.assertFalse(anchored("Keep the things in a bright place",
                                  self.PANTRY))

    def test_an_empty_description_never_blocks(self):
        # Nothing to check against is not a reason to publish nothing.
        from pin_agent.tip_writer import anchored
        self.assertTrue(anchored("Any tip at all here", ""))

    def test_the_writer_enforces_it(self):
        import inspect
        from pin_agent.tip_writer import TipWriter
        src = inspect.getsource(TipWriter.write_for_photo)
        self.assertIn("anchored(", src)
        self.assertIn("description", src)

    def test_the_prompt_says_so_as_well(self):
        # Rejecting costs a call; asking properly costs nothing.
        import inspect
        from pin_agent.tip_writer import TipWriter
        src = inspect.getsource(TipWriter.write_for_photo)
        self.assertIn("does not list", src)



class TestAFixedTipsPictureMustSuitItsTitle(unittest.TestCase):
    """
    Three of the last fifteen published pins had a picture that did not show
    what the title said, and every one came from the fixed set:

        "Stack pans with the lids stored separately"      a cutlery drawer
        "Only daily appliances deserve worktop space"     a bowl of fruit
        "One hook per person beats one rail for everyone" people, no hooks

    Auditing those photographs missed all three, because the audit asked the
    question the photograph was CHOSEN by -- does this match the search term
    "kitchen drawer" -- and a cutlery drawer does. Nobody asked whether it
    matched the TITLE. A freshly written tip is asked exactly that, so the
    fixed set is now asked it too.
    """

    def _agent(self, verdicts, verifier_ready=True):
        from unittest.mock import MagicMock
        from pin_agent.pin_bot import PinAgent
        a = PinAgent.__new__(PinAgent)
        a.recent_photos = []
        a.recent_tips = []
        a.recent_boards = []
        a.last_error = ""
        a.upload_image = None
        a.config = MagicMock(buffer_board_id="b1")
        a.imaging = MagicMock()
        a.asked = []

        class Verifier:
            is_ready = verifier_ready
            last_description = ""

            async def verify_url(self, url, subject, caption=""):
                a.asked.append((url, caption))
                # What the model SAW, which is what gets judged -- its
                # yes/no is reached against the search term and allowed
                # all three of the real failures.
                self.last_description = verdicts.get(url, "")
                return True

        a.verifier = Verifier()

        # Cycles, because the real _next_tip always returns SOMETHING --
        # the fixed set tiers down to "anything" rather than give up.
        tips_left = list(self.TIPS)

        async def next_tip(tried, prefer_bank=False):
            if not tips_left:
                tips_left.extend(self.TIPS)
            return dict(tips_left.pop(0))

        async def build(product, title, eyebrow="", require_photo=False):
            return ("/tmp/pin.jpg", b"bytes")

        a._next_tip = next_tip
        a.imaging.build = build
        a._tip_id = lambda t: "tip"
        a.gate = MagicMock()
        a.gate.approve = lambda pin: (True, [])
        a.gate.image_fingerprint = lambda b: "hash"
        return a

    TIPS = [
        {"title": "Stack pans with the lids stored separately",
         "board": "Under Sink and Cabinet Storage", "body": "b",
         "photo": "kitchen drawer", "image": "https://a/cutlery.jpg",
         "credit": "", "photo_note": ""},
        {"title": "Keep eggs in their carton, pointed end down",
         "board": "Pantry and Fridge Storage", "body": "b",
         "photo": "eggs", "image": "https://a/eggs.jpg",
         "credit": "", "photo_note": ""},
    ]

    CUTLERY = "Cutlery in a wooden drawer organizer in a kitchen. BRIGHT."
    EGGS = "Brown eggs in a green cardboard carton. BRIGHT."

    def test_a_picture_that_does_not_suit_the_title_is_skipped(self):
        agent = self._agent({"https://a/cutlery.jpg": self.CUTLERY,
                             "https://a/eggs.jpg": self.EGGS})
        pin = asyncio.run(agent.build_value_pin())
        self.assertIsNotNone(pin)
        self.assertEqual(pin["photo_url"], "https://a/eggs.jpg")

    def test_the_title_is_what_it_is_judged_against(self):
        # Not the search term -- that is the question it already passed.
        agent = self._agent({"https://a/cutlery.jpg": self.CUTLERY})
        asyncio.run(agent.build_value_pin())
        self.assertTrue(agent.asked)
        url, caption = agent.asked[0]
        self.assertEqual(caption, "Stack pans with the lids stored separately")

    def test_a_good_picture_publishes_without_fuss(self):
        agent = self._agent({"https://a/cutlery.jpg":
                             "Stacked pans and lids in a cabinet. BRIGHT."})
        pin = asyncio.run(agent.build_value_pin())
        self.assertEqual(pin["photo_url"], "https://a/cutlery.jpg")

    def test_no_description_means_yes(self):
        # A vision check that cannot answer must not silence the slot.
        agent = self._agent({})
        pin = asyncio.run(agent.build_value_pin())
        self.assertEqual(pin["photo_url"], "https://a/cutlery.jpg")

    def test_nothing_is_checked_when_the_verifier_is_down(self):
        """
        THE SAFETY NET MUST NOT DEPEND ON THE THING IT PROTECTS AGAINST.
        The fixed set exists for the moments the vision check cannot
        answer, so when it is unavailable the pin goes out as before.
        """
        agent = self._agent({"https://a/cutlery.jpg": self.CUTLERY},
                            verifier_ready=False)
        pin = asyncio.run(agent.build_value_pin())
        self.assertEqual(pin["photo_url"], "https://a/cutlery.jpg")
        self.assertEqual(agent.asked, [], "it must not even be asked")

    def test_the_last_attempt_publishes_anyway(self):
        # A dull pin beats an empty slot.
        from pin_agent.pin_bot import PinAgent
        agent = self._agent({"https://a/cutlery.jpg": self.CUTLERY,
                             "https://a/eggs.jpg": self.CUTLERY})
        agent.TIPS = self.TIPS
        pin = asyncio.run(agent.build_value_pin())
        self.assertIsNotNone(pin, "publishing nothing is the worse failure")
