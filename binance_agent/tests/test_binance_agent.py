"""
Tests for the Binance Square agent.

Standard library only, no network. Every external call is faked, so this is
safe to run at any time.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from binance_agent.bot import BinanceAgent, DRAFT_SLOTS
from binance_agent.config import BinanceConfig
from binance_agent.delivery import DraftDelivery
from binance_agent.market import (BinanceMarket, EXCLUDE_SUFFIXES,
                                  PRIMARY, STABLECOINS)
from binance_agent.writer import DISCLAIMER, PostWriter


def ticker(symbol, change, volume, trades, last=100.0, high=110.0, low=90.0):
    return {"symbol": symbol, "priceChangePercent": str(change),
            "quoteVolume": str(volume), "count": trades,
            "lastPrice": str(last), "highPrice": str(high), "lowPrice": str(low)}


class TestMarketHost(unittest.TestCase):
    """
    Binance answers 451 to US IPs on api.binance.com, and the bot runs on
    Render in San Francisco -- so the obvious host works on a laptop in
    Lahore and fails silently in production.
    """

    def test_the_public_data_host_is_used(self):
        self.assertEqual(PRIMARY, "https://data-api.binance.vision")
        self.assertEqual(BinanceMarket().host, PRIMARY)

    def test_no_api_key_anywhere(self):
        """Read-only by construction. Nothing here could move money."""
        import inspect
        src = inspect.getsource(BinanceMarket)
        for word in ("api_key", "apiKey", "secret", "signature", "X-MBX-APIKEY"):
            self.assertNotIn(word, src)


class TestSelection(unittest.TestCase):

    def setUp(self):
        self.m = BinanceMarket(min_quote_volume=20_000_000)

    def test_stablecoins_are_excluded(self):
        """A 0.02% wobble on USDC is not a story, and its volume is huge."""
        for coin in ("USDC", "FDUSD", "TUSD", "DAI"):
            self.assertIn(coin, STABLECOINS)
            self.assertFalse(self.m._tradeable(
                ticker(f"{coin}USDT", 0.02, 2_000_000_000, 500_000)))

    def test_leveraged_tokens_are_excluded(self):
        """They move for reasons that have nothing to do with the asset."""
        for sym in ("BTCUPUSDT", "ETHDOWNUSDT", "BTC3LUSDT", "ETHBULLUSDT"):
            self.assertFalse(self.m._tradeable(
                ticker(sym, 30.0, 90_000_000, 400_000)))

    def test_a_real_coin_containing_those_letters_survives(self):
        """
        Regression: the marker was matched as a SUBSTRING, so SUPER -- which
        contains "UP" -- was thrown away, along with any coin unlucky enough
        to hold two of those letters anywhere in its name.
        """
        for sym in ("SUPERUSDT", "PUMPUSDT", "BEARNUSDT", "UPSIDEUSDT"):
            self.assertTrue(
                self.m._tradeable(ticker(sym, 9.0, 60_000_000, 300_000)),
                f"wrongly excluded {sym}")

    def test_thin_books_are_excluded(self):
        """+40% on two million dollars is one buyer, not a market."""
        self.assertFalse(self.m._tradeable(
            ticker("TINYUSDT", 40.0, 2_000_000, 900)))
        self.assertTrue(self.m._tradeable(
            ticker("REALUSDT", 8.0, 60_000_000, 300_000)))

    def test_only_usdt_pairs(self):
        self.assertFalse(self.m._tradeable(ticker("ETHBTC", 5.0, 90_000_000, 400_000)))

    def test_a_fall_ranks_as_high_as_a_rise(self):
        """
        Ranking only gainers produces a permanently bullish feed that stops
        being credible the first week the market drops.
        """
        async def fake(path, params=None):
            return [ticker("AAAUSDT", 12.0, 60_000_000, 300_000),
                    ticker("BBBUSDT", -12.0, 60_000_000, 300_000)]
        self.m._get = fake
        rows = asyncio.run(self.m.movers())
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["score"], rows[1]["score"], places=4)

    def test_volume_breaks_a_tie(self):
        async def fake(path, params=None):
            return [ticker("THINUSDT", 10.0, 25_000_000, 60_000),
                    ticker("DEEPUSDT", 10.0, 900_000_000, 3_000_000)]
        self.m._get = fake
        rows = asyncio.run(self.m.movers())
        self.assertEqual(rows[0]["symbol"], "DEEPUSDT")

    def test_range_position_is_computed(self):
        async def fake(path, params=None):
            return [ticker("XUSDT", 9.0, 60_000_000, 300_000,
                           last=109.0, high=110.0, low=90.0)]
        self.m._get = fake
        rows = asyncio.run(self.m.movers())
        self.assertGreater(rows[0]["range_position"], 0.9)

    def test_bad_data_never_raises(self):
        async def fake(path, params=None):
            return [{"symbol": "XUSDT"}, None, "nonsense",
                    ticker("OKUSDT", 5.0, 60_000_000, 300_000)]
        self.m._get = fake
        rows = asyncio.run(self.m.movers())
        self.assertEqual([r["symbol"] for r in rows], ["OKUSDT"])

    def test_no_data_returns_empty(self):
        async def fake(path, params=None):
            return None
        self.m._get = fake
        self.assertEqual(asyncio.run(self.m.movers()), [])


class TestTheGate(unittest.TestCase):
    """Deterministic, never a model. The failure mode is telling strangers
    to buy something."""

    def good(self, extra=""):
        return ("SOL traded between $180 and $196 over the last 24 hours on "
                "$244 million of volume across 968,187 trades, closing "
                "mid-range. " + extra + "\n\nWhat are you watching?\n\n$SOL"
                "\n\n" + DISCLAIMER)

    def test_a_normal_post_passes(self):
        self.assertEqual(PostWriter.check(self.good()), "")

    def test_the_disclaimer_does_not_reject_itself(self):
        """
        Regression, and it rejected EVERY post on the first dry run: the
        disclaimer says "Not financial advice" and the scanner bans the
        phrase "financial advice".
        """
        self.assertIn("financial advice", DISCLAIMER.lower())
        self.assertEqual(PostWriter.check(self.good()), "")

    def test_advice_is_refused(self):
        for phrase in ("You should buy this now.", "It is guaranteed to rise.",
                       "This is easy money.", "It will definitely recover.",
                       "To the moon!", "This is risk-free."):
            self.assertNotEqual(PostWriter.check(self.good(phrase)), "",
                                f"allowed: {phrase!r}")

    def test_price_targets_are_refused(self):
        """A prediction ages into a false claim."""
        self.assertNotEqual(PostWriter.check(self.good("Target: $250.")), "")

    def test_a_missing_disclaimer_is_refused(self):
        self.assertNotEqual(
            PostWriter.check(self.good().replace(DISCLAIMER, "")), "")

    def test_a_missing_cashtag_is_refused(self):
        """Square files a post on the coin's page by its cashtag. Without one
        it reaches nobody."""
        self.assertNotEqual(PostWriter.check(self.good().replace("$SOL", "SOL")), "")

    def test_a_post_with_no_question_is_refused(self):
        """Square ranks on comments, and nothing to answer means none."""
        self.assertNotEqual(
            PostWriter.check(self.good().replace("What are you watching?", "")), "")

    def test_too_short_is_refused(self):
        self.assertNotEqual(PostWriter.check("SOL up. $SOL " + DISCLAIMER), "")


class TestWriter(unittest.TestCase):

    COIN = {"symbol": "SOLUSDT", "base": "SOL", "change": 8.4, "price": 190.0,
            "high": 196.0, "low": 180.0, "volume": 244_000_000,
            "trades": 968_187, "score": 26.6, "range_position": 0.62}

    def test_it_writes_without_a_model_at_all(self):
        """
        A model outage must not cost the day. The numbers ARE the post; the
        fallback is drier and still accurate.
        """
        w = PostWriter(ai_engine=None)
        draft = asyncio.run(w.write(self.COIN, {}))
        self.assertIsNotNone(draft)
        self.assertEqual(PostWriter.check(draft["text"]), "")
        self.assertIn("$SOL", draft["text"])
        self.assertIn("968,187", draft["text"])

    def test_a_model_that_gives_advice_falls_back(self):
        ai = MagicMock()
        ai.generate = AsyncMock(return_value="You should buy SOL now, guaranteed.")
        draft = asyncio.run(PostWriter(ai_engine=ai).write(self.COIN, {}))
        self.assertIsNotNone(draft)
        self.assertNotIn("should buy", draft["text"].lower())
        self.assertEqual(PostWriter.check(draft["text"]), "")

    def test_a_model_failure_falls_back(self):
        ai = MagicMock()
        ai.generate = AsyncMock(side_effect=RuntimeError("groq down"))
        draft = asyncio.run(PostWriter(ai_engine=ai).write(self.COIN, {}))
        self.assertIsNotNone(draft)
        self.assertEqual(PostWriter.check(draft["text"]), "")

    def test_the_facts_are_computed_not_asked_for(self):
        """A model asked for a price invents one. It is handed them."""
        facts = PostWriter.facts(self.COIN, {"week_change": 12.0,
                                             "month_change": 30.0,
                                             "at_month_high": False,
                                             "at_month_low": False})
        self.assertIn("8.4%", facts)
        self.assertIn("968,187", facts)
        self.assertIn("+12.0%", facts)


class TestAgent(unittest.TestCase):

    def _agent(self, **kw):
        cfg = BinanceConfig(draft_group="-1001", drafts_per_day=kw.pop("per_day", 3),
                            repeat_after_days=kw.pop("repeat_days", 3))
        a = BinanceAgent(config=cfg, ai_engine=None)
        a.active = True
        a.market.movers = AsyncMock(return_value=kw.pop("movers", [
            {"symbol": "SOLUSDT", "base": "SOL", "change": 8.4, "price": 190.0,
             "high": 196.0, "low": 180.0, "volume": 244_000_000,
             "trades": 968_187, "score": 26.6, "range_position": 0.6}]))
        a.market.context = AsyncMock(return_value={})
        a.delivery.send = AsyncMock(return_value=True)
        a.delivery.announce = AsyncMock(return_value=True)
        return a

    def test_off_by_default(self):
        self.assertFalse(BinanceAgent(config=BinanceConfig()).active)

    def test_it_writes_a_draft(self):
        a = self._agent()
        d = asyncio.run(a.run_slot())
        self.assertIsNotNone(d)
        self.assertEqual(d["base"], "SOL")

    def test_nothing_happens_while_off(self):
        a = self._agent()
        a.active = False
        self.assertIsNone(asyncio.run(a.run_slot()))
        a.delivery.send.assert_not_awaited()

    def test_the_same_coin_is_not_covered_twice_in_a_row(self):
        """Otherwise BTC and ETH win every day -- they carry the volume."""
        a = self._agent()
        asyncio.run(a.run_slot())
        self.assertIsNone(asyncio.run(a.build_one()))

    def test_the_repeat_guard_expires(self):
        a = self._agent(repeat_days=3)
        asyncio.run(a.run_slot())
        a._recent[0]["at"] = datetime.now(timezone.utc) - timedelta(days=5)
        self.assertIsNotNone(asyncio.run(a.build_one()))

    def test_a_small_move_is_not_a_story(self):
        a = self._agent(movers=[
            {"symbol": "BTCUSDT", "base": "BTC", "change": 0.8, "price": 90000.0,
             "high": 91000.0, "low": 89000.0, "volume": 1e9, "trades": 3_000_000,
             "score": 5.0, "range_position": 0.5}])
        self.assertIsNone(asyncio.run(a.build_one()))

    def test_the_daily_cap_holds(self):
        a = self._agent(per_day=1)
        a._too_soon = lambda base: False        # allow the same coin again
        asyncio.run(a.run_slot())
        self.assertIsNone(asyncio.run(a.run_slot()))
        self.assertEqual(a.delivery.send.await_count, 1)

    def test_a_quiet_market_is_said_out_loud(self):
        """Silence is indistinguishable from a crash."""
        a = self._agent(movers=[])
        asyncio.run(a.run_slot())
        a.delivery.announce.assert_awaited()

    def test_slots_are_hours_a_person_is_awake(self):
        """The constraint is the human who pastes it, not the market."""
        for hour, _ in DRAFT_SLOTS:
            self.assertTrue(8 <= hour <= 23, f"{hour}:00 PKT is not a waking hour")


class TestDelivery(unittest.TestCase):

    def test_nothing_is_sent_without_a_group(self):
        d = DraftDelivery(client_owner=MagicMock(), group="")
        self.assertFalse(d.is_ready)
        self.assertFalse(asyncio.run(d.send({"base": "SOL", "text": "x",
                                             "facts": "y", "score": 1})))

    def test_nothing_is_sent_without_a_client(self):
        self.assertFalse(DraftDelivery(client_owner=None, group="-100").is_ready)

    def test_html_is_escaped(self):
        """An unescaped < in a post breaks Telegram's parser and the draft
        never arrives."""
        from binance_agent.delivery import _esc
        self.assertEqual(_esc("a < b & c > d"), "a &lt; b &amp; c &gt; d")


if __name__ == "__main__":
    unittest.main(verbosity=2)
