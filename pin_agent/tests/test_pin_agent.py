"""
Tests for the Pinterest agent.

Runs entirely offline — no AliExpress key, no Pinterest app, no network. The
compliance tests matter most: a pin that breaks Pinterest's affiliate rules
risks the account, and that is not recoverable by apologising.

    python -m unittest pin_agent.tests.test_pin_agent
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from pin_agent.compliance import ComplianceGate
from pin_agent.selector import ProductSelector
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
