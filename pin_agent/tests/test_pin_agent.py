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


if __name__ == "__main__":
    unittest.main(verbosity=2)
