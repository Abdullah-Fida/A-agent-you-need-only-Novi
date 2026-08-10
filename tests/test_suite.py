"""
Full automated test suite for the Novi bot.

Uses only the standard library (unittest) so it runs anywhere with no extra
installs. Nothing here touches Telegram, sends email, or writes to Supabase —
every external service is faked, so the suite is safe to run at any time.

Run:
    python -m tests.test_suite          (from the omni_channel_bot folder)
    python tests/test_suite.py
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.brain import BotBrain
from core.ai_engine import AIEngine, MODELS, FALLBACK_MODELS
from modules.growth_engine import GrowthEngine
from modules.signal_copier import SignalCopier
from modules.stealth_marketer import StealthMarketer
from modules.notification_manager import NotificationManager
from utils.telegram_utils import candidate_ids

PKT = timedelta(hours=5)


def make_brain(**kw):
    """Builds a Brain without touching the database."""
    cfg = MagicMock()
    cfg.max_daily_posts = kw.get("max_posts", 6)
    cfg.max_daily_telegram_replies = kw.get("max_replies", 8)
    cfg.max_daily_reddit_posts = kw.get("max_reddit", 2)
    cfg.max_daily_x_posts = kw.get("max_x", 5)
    cfg.sleep_start_hour = kw.get("sleep_start", 23)
    cfg.sleep_end_hour = kw.get("sleep_end", 7)
    return BotBrain(db=None, weekly_goal=kw.get("goal", 100), config=cfg)


# ═══════════════════════════════════════════════════════════════
#  BRAIN — scheduling, sleep, limits
# ═══════════════════════════════════════════════════════════════
class TestBrainSleep(unittest.TestCase):

    def test_is_sleeping_is_property_not_callable_bool(self):
        """Regression: stealth_marketer called brain.is_sleeping() and crashed."""
        self.assertIsInstance(BotBrain.is_sleeping, property)
        self.assertIsInstance(make_brain().is_sleeping, bool)

    def test_overnight_window(self):
        b = make_brain(sleep_start=23, sleep_end=7)
        for hour, expected in [(23, True), (0, True), (3, True), (6, True),
                               (7, False), (12, False), (22, False)]:
            with patch.object(b, "_get_pkt_now",
                              return_value=datetime(2026, 1, 1, hour, 0, tzinfo=timezone.utc)):
                self.assertEqual(b.is_sleep_time(), expected, f"hour {hour}")

    def test_same_day_window(self):
        b = make_brain(sleep_start=1, sleep_end=5)
        for hour, expected in [(0, False), (1, True), (4, True), (5, False), (23, False)]:
            with patch.object(b, "_get_pkt_now",
                              return_value=datetime(2026, 1, 1, hour, 0, tzinfo=timezone.utc)):
                self.assertEqual(b.is_sleep_time(), expected, f"hour {hour}")

    def test_always_on_mode(self):
        b = make_brain()
        b.set_sleep_window(0, 0)
        for hour in range(24):
            with patch.object(b, "_get_pkt_now",
                              return_value=datetime(2026, 1, 1, hour, 0, tzinfo=timezone.utc)):
                self.assertFalse(b.is_sleep_time(), f"should never sleep at {hour}")

    def test_sleep_window_clamped_to_valid_hours(self):
        b = make_brain()
        b.set_sleep_window(99, -5)
        self.assertEqual(b.SCHEDULE["sleep_start"], 23)
        self.assertEqual(b.SCHEDULE["sleep_end"], 0)


class TestBrainSchedule(unittest.TestCase):

    def test_slots_in_same_hour_have_distinct_keys(self):
        """Regression: 13:00 and 13:40 collapsed into one slot; 13:40 never fired."""
        b = make_brain()
        keys = set()
        for slot in b.SCHEDULE["post_slots"]:
            with patch.object(b, "_get_pkt_now",
                              return_value=datetime(2026, 1, 1, slot["hour"], slot["minute"],
                                                    tzinfo=timezone.utc)):
                got = b.get_next_post_slot()
                self.assertIsNotNone(got, f"slot {slot} did not trigger")
                keys.add(got["key"])
        self.assertEqual(len(keys), len(b.SCHEDULE["post_slots"]))

    def test_slot_window_covers_max_jitter(self):
        """A post must not be jittered past its own slot deadline."""
        self.assertGreater(BotBrain.SLOT_WINDOW_MINUTES * 60, 600)

    def test_no_slot_outside_window(self):
        b = make_brain()
        with patch.object(b, "_get_pkt_now",
                          return_value=datetime(2026, 1, 1, 3, 17, tzinfo=timezone.utc)):
            self.assertIsNone(b.get_next_post_slot())


class TestBrainLimits(unittest.TestCase):

    def test_configured_limits_are_applied(self):
        """Regression: MAX_DAILY_* were configured but hardcoded values were used."""
        b = make_brain(max_posts=11, max_replies=9, max_reddit=4, max_x=7)
        self.assertEqual(b.max_posts_today, 11)
        self.assertEqual(b.max_replies_today, 9)
        self.assertEqual(b.max_reddit_posts_today, 4)
        self.assertEqual(b.max_x_posts_today, 7)

    def test_reddit_and_x_limits_enforced(self):
        b = make_brain(max_reddit=2, max_x=1)
        self.assertTrue(b.can_post_reddit())
        b.record_reddit_post(); b.record_reddit_post()
        self.assertFalse(b.can_post_reddit())

        self.assertTrue(b.can_post_x())
        b.record_x_post()
        self.assertFalse(b.can_post_x())

    def test_post_limit_enforced(self):
        b = make_brain(max_posts=2)
        self.assertTrue(b.can_post())
        b.record_post(); b.record_post()
        self.assertFalse(b.can_post())

    def test_midnight_reset_restores_configured_default(self):
        """Regression: reset hardcoded max_replies back to 8, ignoring config."""
        b = make_brain(max_replies=15)
        b.max_replies_today = 3
        b.posts_today = 5
        b.reddit_posts_today = 2
        b.reset_daily_counters()
        self.assertEqual(b.max_replies_today, 15)
        self.assertEqual(b.posts_today, 0)
        self.assertEqual(b.reddit_posts_today, 0)


# ═══════════════════════════════════════════════════════════════
#  TELEGRAM ID RESOLUTION
# ═══════════════════════════════════════════════════════════════
class TestTelegramIds(unittest.TestCase):

    def test_supergroup_missing_100_prefix_is_corrected(self):
        """Regression: -5533411583 never resolved; posting silently did nothing."""
        self.assertEqual(candidate_ids("-5533411583")[0], -1005533411583)

    def test_correct_id_stays_first(self):
        self.assertEqual(candidate_ids("-1005533411583")[0], -1005533411583)

    def test_original_value_kept_as_fallback(self):
        self.assertIn(-5533411583, candidate_ids("-5533411583"))

    def test_username_passthrough(self):
        self.assertEqual(candidate_ids("@Chan"), ["@Chan"])

    def test_link_normalized(self):
        self.assertEqual(candidate_ids("https://t.me/Chan")[0], "@Chan")

    def test_bare_name_gets_at_prefix(self):
        self.assertEqual(candidate_ids("Chan")[0], "@Chan")

    def test_private_invite_link_untouched(self):
        self.assertEqual(candidate_ids("https://t.me/+AbCd")[0], "https://t.me/+AbCd")

    def test_empty(self):
        self.assertEqual(candidate_ids(""), [])
        self.assertEqual(candidate_ids(None), [])


# ═══════════════════════════════════════════════════════════════
#  AI ENGINE
# ═══════════════════════════════════════════════════════════════
class TestAIEngine(unittest.TestCase):

    def test_article_task_has_model(self):
        """Regression: ArticleAgent used task='article' which wasn't mapped."""
        self.assertIn("article", MODELS)

    def test_dead_fallback_model_removed(self):
        self.assertNotIn("meta-llama/llama-3.1-8b-instruct:free", FALLBACK_MODELS)

    def test_single_key_rotation_does_not_crash(self):
        eng = AIEngine.__new__(AIEngine)
        eng.api_keys = ["k"]
        eng.current_key_index = 0
        self.assertFalse(eng._rotate_key())
        self.assertEqual(eng.current_key_index, 0)

    def test_multi_key_rotation_cycles(self):
        eng = AIEngine.__new__(AIEngine)
        eng.api_keys = ["a", "b", "c"]
        eng.current_key_index = 0
        eng._build_client = MagicMock()
        self.assertTrue(eng._rotate_key())
        self.assertEqual(eng.current_key_index, 1)
        self.assertTrue(eng._rotate_key())
        self.assertEqual(eng.current_key_index, 2)
        self.assertFalse(eng._rotate_key())   # wrapped
        self.assertEqual(eng.current_key_index, 0)

    def test_generate_retries_then_returns_none(self):
        """A persistent API failure must return None, not raise."""
        # Real constructor so every attribute (default_model, label, base_url)
        # is initialised the way production does it.
        eng = AIEngine(api_keys=["k"], label="TestAI")
        eng._build_client = MagicMock()
        eng.client = MagicMock()
        eng.client.chat.completions.create = AsyncMock(side_effect=Exception("500 server error"))

        with patch.object(AIEngine, "_backoff", new=AsyncMock()):
            result = asyncio.run(eng.generate("synthesizer", "sys", "user"))
        self.assertIsNone(result)
        self.assertGreaterEqual(eng.client.chat.completions.create.await_count, 3)

    def test_generate_succeeds_on_retry(self):
        # Real constructor so every attribute (default_model, label, base_url)
        # is initialised the way production does it.
        eng = AIEngine(api_keys=["k"], label="TestAI")
        eng._build_client = MagicMock()
        eng.client = MagicMock()

        good = MagicMock()
        good.choices = [MagicMock(message=MagicMock(content="Real content here."))]
        eng.client.chat.completions.create = AsyncMock(
            side_effect=[Exception("429 rate limit"), good])

        with patch.object(AIEngine, "_backoff", new=AsyncMock()):
            result = asyncio.run(eng.generate("synthesizer", "sys", "user"))
        self.assertEqual(result, "Real content here.")

    def test_long_article_not_rejected_by_quality_gate(self):
        """The gate must only inspect the opening, not the whole body."""
        # Real constructor so every attribute (default_model, label, base_url)
        # is initialised the way production does it.
        eng = AIEngine(api_keys=["k"], label="TestAI")
        eng._build_client = MagicMock()
        eng.client = MagicMock()

        body = "<h2>Markets today</h2><p>" + ("Analysis. " * 60) + "your job is important</p>"
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(content=body))]
        eng.client.chat.completions.create = AsyncMock(return_value=resp)

        with patch.object(AIEngine, "_backoff", new=AsyncMock()):
            result = asyncio.run(eng.generate("article", "sys", "user"))
        self.assertIsNotNone(result)


# ═══════════════════════════════════════════════════════════════
#  SIGNAL COPIER
# ═══════════════════════════════════════════════════════════════
def make_copier(**kw):
    sc = SignalCopier.__new__(SignalCopier)
    sc.__init__(api_id=1, api_hash="h", session_string="", phone="",
                source_channels=kw.get("sources", ["@src"]),
                ai_engine=MagicMock(),
                signal_target_group=kw.get("target", "-5533411583"))
    return sc


class TestSignalCopier(unittest.TestCase):

    def test_starts_inactive_and_unlimited(self):
        sc = make_copier()
        self.assertFalse(sc.is_active)
        self.assertEqual(sc.max_signals_per_day, 0)

    def test_daily_limit_setter(self):
        sc = make_copier()
        sc.set_daily_limit(30)
        self.assertEqual(sc.max_signals_per_day, 30)
        sc.set_daily_limit(-1)
        self.assertEqual(sc.max_signals_per_day, 0)

    def test_duplicate_signal_dropped(self):
        sc = make_copier()
        sc._active = True
        sc._cleanse_signal = AsyncMock(return_value="CLEAN")
        sc._post_to_group = AsyncMock(return_value=True)

        text = "BTC LONG entry 60000 target 62000 stop 59000 leverage 10x"
        ev = MagicMock()
        ev.message.message = text
        ev.message.media = None

        with patch("asyncio.sleep", new=AsyncMock()):
            asyncio.run(sc._handle_new_signal(ev))
            asyncio.run(sc._handle_new_signal(ev))   # identical -> must be ignored

        self.assertEqual(sc._post_to_group.await_count, 1)

    def test_daily_limit_blocks_extra_signals(self):
        sc = make_copier()
        sc._active = True
        sc.set_daily_limit(1)
        sc.signals_copied_today = 1
        sc._cleanse_signal = AsyncMock(return_value="CLEAN")
        sc._post_to_group = AsyncMock(return_value=True)

        ev = MagicMock()
        ev.message.message = "BTC LONG entry 60000 target 62000 stop loss 59000"
        ev.message.media = None

        with patch("asyncio.sleep", new=AsyncMock()):
            asyncio.run(sc._handle_new_signal(ev))
        sc._post_to_group.assert_not_awaited()

    def test_rejected_signal_not_posted(self):
        sc = make_copier()
        sc._active = True
        sc._cleanse_signal = AsyncMock(return_value="REJECT")
        sc._post_to_group = AsyncMock(return_value=True)

        ev = MagicMock()
        ev.message.message = "Join our VIP channel for locked signals right now!!"
        ev.message.media = None

        with patch("asyncio.sleep", new=AsyncMock()):
            asyncio.run(sc._handle_new_signal(ev))
        sc._post_to_group.assert_not_awaited()

    def test_short_message_ignored(self):
        sc = make_copier()
        sc._active = True
        sc._cleanse_signal = AsyncMock()
        ev = MagicMock()
        ev.message.message = "hi"
        asyncio.run(sc._handle_new_signal(ev))
        sc._cleanse_signal.assert_not_awaited()

    def test_status_shape(self):
        sc = make_copier()
        for key in ("active", "signals_copied_today", "max_signals_per_day",
                    "target_resolved", "sources_listening", "connected"):
            self.assertIn(key, sc.status)


# ═══════════════════════════════════════════════════════════════
#  STEALTH MARKETER
# ═══════════════════════════════════════════════════════════════
def make_stealth(phone="+923001234567", brain=None):
    sm = StealthMarketer.__new__(StealthMarketer)
    sm.__init__(api_id=1, api_hash="h", stealth_phone=phone,
                ai_engine=MagicMock(), brain=brain or make_brain(),
                channel_username="@Chan", target_groups=["@g"],
                stealth_invite_group="-5533411583")
    return sm


class TestStealthMarketer(unittest.TestCase):

    def test_starts_inactive(self):
        sm = make_stealth()
        self.assertFalse(sm.is_active)
        self.assertFalse(sm._scraping_active)
        self.assertFalse(sm._reply_active)

    def test_invite_limit_hard_capped(self):
        sm = make_stealth()
        res = sm.set_daily_invite_limit(100000)
        self.assertTrue(res["capped"])
        self.assertEqual(sm._invites_max_per_day, sm.INVITE_HARD_CAP)

    def test_invite_limit_raise_within_cap(self):
        sm = make_stealth()
        res = sm.set_daily_invite_limit(12)
        self.assertFalse(res["capped"])
        self.assertEqual(sm._invites_max_per_day, 12)

    def test_device_fingerprint_stable_across_restarts(self):
        """A phone that changes model each boot is an obvious bot signal."""
        models = {make_stealth()._device["model"] for _ in range(8)}
        self.assertEqual(len(models), 1)

    def test_emergency_stop_blocks_activation(self):
        sm = make_stealth()
        sm._emergency_stop = True
        sm.nm = None
        self.assertFalse(asyncio.run(sm.activate()))
        self.assertFalse(sm.is_active)

    def test_no_force_add_in_source(self):
        """We invite by consent; we never force-add strangers."""
        path = os.path.join(os.path.dirname(__file__), "..", "modules", "stealth_marketer.py")
        with open(path, encoding="utf-8") as fh:
            self.assertNotIn("AddChatUserRequest", fh.read())

    def test_daily_rollover_resets_counter(self):
        sm = make_stealth()
        sm._invites_today = 9
        sm._invite_day = (datetime.now(timezone.utc) + PKT).date() - timedelta(days=1)
        sm._roll_day_if_needed()
        self.assertEqual(sm._invites_today, 0)

    def test_no_rollover_within_same_day(self):
        sm = make_stealth()
        sm._invites_today = 3
        sm._invite_day = (datetime.now(timezone.utc) + PKT).date()
        sm._roll_day_if_needed()
        self.assertEqual(sm._invites_today, 3)

    def test_cycle_skips_during_sleep_hours(self):
        brain = make_brain(sleep_start=23, sleep_end=7)
        sm = make_stealth(brain=brain)
        sm._active = True
        sm._scraping_active = True
        sm._scrape_group_members = AsyncMock(return_value=[1, 2, 3])

        with patch.object(brain, "_get_pkt_now",
                          return_value=datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)):
            asyncio.run(sm.scrape_and_invite_cycle())
        sm._scrape_group_members.assert_not_awaited()

    def test_cycle_respects_invite_spacing(self):
        import time as _t
        brain = make_brain(sleep_start=0, sleep_end=0)   # always awake
        sm = make_stealth(brain=brain)
        sm._active = True
        sm._scraping_active = True
        sm._last_invite_time = _t.time()      # just invited
        sm._scrape_group_members = AsyncMock(return_value=[1])

        asyncio.run(sm.scrape_and_invite_cycle())
        sm._scrape_group_members.assert_not_awaited()

    def test_reply_blocked_while_sleeping(self):
        """Regression: this path raised TypeError('bool' object is not callable)."""
        brain = make_brain(sleep_start=0, sleep_end=0)
        sm = make_stealth(brain=brain)
        sm._active = True
        sm._reply_active = True
        sm._generate_stealth_reply = AsyncMock(return_value="hi")
        sm._send_reply_with_jitter = AsyncMock()

        ev = MagicMock()
        ev.message.message = "what is happening with bitcoin today?"

        with patch.object(brain, "is_sleep_time", return_value=True):
            asyncio.run(sm._handle_new_message(ev))   # must not raise
        sm._send_reply_with_jitter.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════
#  GROWTH ENGINE
# ═══════════════════════════════════════════════════════════════
class TestGrowthEngine(unittest.TestCase):

    def test_tracks_subscribers(self):
        g = GrowthEngine(weekly_goal=100)
        asyncio.run(g.record_subscribers(500))
        self.assertEqual(g.last_count, 500)
        self.assertEqual(g.week_start_count, 500)

    def test_growth_measured_over_time(self):
        g = GrowthEngine(weekly_goal=100)
        now = g._now()
        g._samples.append((now - timedelta(hours=30), 100))
        g.last_count = 140
        self.assertEqual(g.growth_since(24), 40)

    def test_growth_unknown_without_history(self):
        g = GrowthEngine(weekly_goal=100)
        self.assertIsNone(g.growth_since(24))

    def test_action_attribution(self):
        g = GrowthEngine(weekly_goal=100)
        g.record_action(g.ACTION_POST)
        g.record_action(g.ACTION_POST)
        g.record_action(g.ACTION_INVITE)
        acts = g.actions_since(24)
        self.assertEqual(acts[g.ACTION_POST], 2)
        self.assertEqual(acts[g.ACTION_INVITE], 1)

    def test_recommendation_detects_stall(self):
        g = GrowthEngine(weekly_goal=100)
        now = g._now()
        g._samples.append((now - timedelta(hours=30), 100))
        g.last_count = 100
        g.week_start_count = 100
        g.week_start_at = now - timedelta(days=2)
        rec = g.recommendation()
        self.assertEqual(rec["status"], "stalled")
        self.assertTrue(rec["suggested_changes"])

    def test_recommendation_when_on_track(self):
        g = GrowthEngine(weekly_goal=70)
        now = g._now()
        g._samples.append((now - timedelta(hours=30), 100))
        g.last_count = 130
        g.week_start_count = 100
        g.week_start_at = now - timedelta(days=2)
        self.assertEqual(g.recommendation()["status"], "on_track")

    def test_summary_shape(self):
        g = GrowthEngine(weekly_goal=100)
        for key in ("subscribers", "growth_24h", "weekly", "recommendation"):
            self.assertIn(key, g.summary())


# ═══════════════════════════════════════════════════════════════
#  NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════
class TestNotifications(unittest.TestCase):

    def test_no_config_does_not_raise(self):
        nm = NotificationManager("", "", "", db=None)
        self.assertFalse(asyncio.run(nm._dispatch("subject", "<p>body</p>")))

    def test_resend_failure_falls_back_to_smtp(self):
        nm = NotificationManager("me@gmail.com", "pw", "you@gmail.com",
                                 resend_api_key="re_x", db=None)
        nm._send_via_resend = MagicMock(return_value=False)
        nm._send_via_smtp = MagicMock(return_value=True)
        self.assertTrue(nm._send_email_sync("s", "<p>b</p>"))
        nm._send_via_smtp.assert_called_once()

    def test_total_failure_is_recorded(self):
        nm = NotificationManager("me@gmail.com", "pw", "you@gmail.com", db=None)
        nm._send_via_smtp = MagicMock(return_value=False)
        self.assertFalse(nm._send_email_sync("lost subject", "<p>b</p>"))
        self.assertEqual(nm.health["failure_count"], 1)

    def test_dispatch_never_raises_on_internal_error(self):
        """A broken inbox must never take down posting."""
        nm = NotificationManager("me@gmail.com", "pw", "you@gmail.com", db=None)
        nm._send_email_sync = MagicMock(side_effect=Exception("boom"))
        self.assertFalse(asyncio.run(nm._dispatch("s", "b")))


# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════
class TestConfig(unittest.TestCase):

    def test_bad_int_falls_back_to_default(self):
        from core.config import _int_env
        with patch.dict(os.environ, {"X_TEST": "not-a-number"}):
            self.assertEqual(_int_env("X_TEST", 7), 7)

    def test_int_clamped_to_range(self):
        from core.config import _int_env
        with patch.dict(os.environ, {"X_TEST": "99"}):
            self.assertEqual(_int_env("X_TEST", 0, lo=0, hi=23), 23)

    def test_quotes_stripped(self):
        from core.config import _clean
        self.assertEqual(_clean('"@Chan"'), "@Chan")
        self.assertEqual(_clean("  spaced  "), "spaced")

    def test_csv_parsing(self):
        from core.config import _csv
        with patch.dict(os.environ, {"X_LIST": " @a , @b ,, @c "}):
            self.assertEqual(_csv("X_LIST"), ["@a", "@b", "@c"])


# ═══════════════════════════════════════════════════════════════
#  CODEBASE INVARIANTS
# ═══════════════════════════════════════════════════════════════
class TestCodebaseInvariants(unittest.TestCase):

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _py_files(self):
        for root, dirs, files in os.walk(self.ROOT):
            dirs[:] = [d for d in dirs
                       if d not in ("node_modules", "__pycache__", ".git", ".next", "tests")]
            for f in files:
                if f.endswith(".py"):
                    yield os.path.join(root, f)

    def test_no_await_on_sync_supabase(self):
        """supabase-py is synchronous; awaiting it silently discards the write."""
        import re
        bad = []
        for p in self._py_files():
            for i, line in enumerate(open(p, encoding="utf-8", errors="replace"), 1):
                if re.search(r"await\s+self\.(db\.)?client\.table", line):
                    bad.append(f"{os.path.relpath(p, self.ROOT)}:{i}")
        self.assertEqual(bad, [], f"await on sync supabase call: {bad}")

    def test_all_files_compile(self):
        import py_compile
        for p in self._py_files():
            with self.subTest(file=os.path.relpath(p, self.ROOT)):
                py_compile.compile(p, doraise=True)

    def test_dashboard_has_no_hardcoded_localhost_calls(self):
        p = os.path.join(self.ROOT, "novi-dashboard", "src", "App.jsx")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("`http://localhost:8000${data.image_url}`", src)
        self.assertNotIn("'http://localhost:8000' + pkg.image_url", src)
        self.assertIn("import.meta.env.VITE_API_BASE", src)

    def test_novi_can_control_every_module(self):
        """Every backend capability must be reachable by voice through NOVI."""
        p = os.path.join(self.ROOT, "novi-dashboard", "src", "App.jsx")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        for action in ("post_now", "invite_now", "growth_report", "health",
                       "set_sleep_window", "stealth_invite", "master_kill"):
            self.assertIn(action, src, f"NOVI cannot control: {action}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
