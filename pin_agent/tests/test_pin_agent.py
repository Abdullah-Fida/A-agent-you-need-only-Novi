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

        agent.store.posted_history = history
        agent.store.posted_today = posted_today
        agent.store.pending_pins = pending
        agent.store.category_performance = performance
        agent.store.first_pin_at = first_pin_at
        agent.store.recent_titles = recent_titles
        agent.store.recent_tip_titles = recent_tip_titles
        agent.store.recent_types = recent_types

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
        source = inspect.getsource(self.PinAgent.build_one)
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
        agent.last_error = ""
        agent.last_run = None
        agent.nm = None
        agent.built = []

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
        agent.last_error = ""
        agent.upload_image = None
        agent.imaging = MagicMock()
        agent.asked = []
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
        src = inspect.getsource(PinAgent.build_one)
        self.assertIn("blocked_by_cooldown", src,
                      "the cooldown must be enforced where pins are built")
        self.assertIn('classify(copy["title"])', src,
                      "classify on the TITLE - descriptions mention other "
                      "products in passing and put a rolling cart in the "
                      "spice bucket")

    def test_the_type_is_what_gets_stored(self):
        import inspect
        from pin_agent.pin_bot import PinAgent
        src = inspect.getsource(PinAgent.build_one)
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
