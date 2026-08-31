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
import tempfile
import re
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


class TestImageGeneration(unittest.TestCase):
    """
    Every published post must carry an image. These lock in the failure modes
    behind a post that reached the channel without one.
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def setUp(self):
        from modules.image_generator import ImageGenerator
        self.tmp = tempfile.mkdtemp()
        self.gen = ImageGenerator(output_dir=self.tmp, channel_name="Novi News",
                                  bing_cookie="")

    def test_generate_is_a_coroutine(self):
        """
        It reaches out over the network, so calling it synchronously from the
        event loop froze the scheduler and Telegram keepalives for minutes.
        """
        self.assertTrue(asyncio.iscoroutinefunction(self.gen.generate))

    def test_content_engine_awaits_image_generation(self):
        p = os.path.join(self.ROOT, "modules", "content_engine.py")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("await self.image_gen.generate(", src)
        self.assertNotIn("= self.image_gen.generate(", src)

    def test_no_blocking_calls_in_image_generator(self):
        """A blocking sleep or worker thread here stalls every other task."""
        p = os.path.join(self.ROOT, "modules", "image_generator.py")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("time.sleep(", src)
        self.assertNotIn("threading.Thread", src)

    def test_bing_is_the_only_generator(self):
        """No second image AI may creep back in — Bing or real photography."""
        p = os.path.join(self.ROOT, "modules", "image_generator.py")
        with open(p, encoding="utf-8") as fh:
            src = fh.read().lower()
        for banned in ("pollinations", "stability.ai", "replicate.com",
                       "openai.com/v1/images", "dall-e-3\", ", "unsplash.com"):
            self.assertNotIn(banned, src, f"a non-Bing image source is present: {banned}")

    def test_card_fallback_always_produces_a_file(self):
        """With Bing dead and no news photo, a branded card must still be saved."""
        self.gen.bing_cookie = "cookie"

        async def dead(*a, **k):
            raise RuntimeError("bing down")
        self.gen._from_bing = dead

        path = asyncio.run(self.gen.generate("Some breaking headline", "crypto"))
        self.assertIsNotNone(path, "generate() returned None though the card tier exists")
        self.assertTrue(os.path.exists(path))
        self.assertGreater(os.path.getsize(path), 2048)
        self.assertEqual(self.gen.last_source, "card")

    def test_news_photo_is_preferred_over_the_card(self):
        """
        When Bing cannot deliver, the photo published with the story is used
        before falling back to a drawn card.
        """
        from PIL import Image as _Image
        self.gen.bing_cookie = "cookie"

        async def dead(*a, **k):
            raise RuntimeError("bing down")
        self.gen._from_bing = dead

        async def photo(url):
            self.assertTrue(url.startswith("http"))
            return _Image.new("RGB", (1600, 900), (40, 80, 120))
        self.gen._from_url = photo

        path = asyncio.run(self.gen.generate(
            "Headline", "world_news", story_image_url="https://news.example/photo.jpg"))
        self.assertIsNotNone(path)
        self.assertEqual(self.gen.last_source, "story_image")

    def test_card_renders_without_any_installed_font(self):
        """
        Render's container may ship no TTF files. Named lookups then fail and
        Pillow's bundled face has to carry the card.
        """
        from PIL import ImageFont
        real = ImageFont.truetype

        def only_named_fonts_are_missing(font=None, size=10, *a, **kw):
            # A str/Path is a file on disk; the bundled face is passed as a
            # file object, and that one must keep working.
            if isinstance(font, (str, bytes, os.PathLike)):
                raise OSError("no fonts installed")
            return real(font, size, *a, **kw)

        with patch("PIL.ImageFont.truetype", side_effect=only_named_fonts_are_missing):
            img = self.gen._branded_card("A headline that must still render", "world_news")
        self.assertEqual(img.size, (1280, 720))

    def test_budget_is_bounded(self):
        """A hung Bing request must not consume the whole posting slot."""
        self.gen.bing_cookie = "cookie"

        async def hang(*a, **k):
            await asyncio.sleep(600)
        self.gen._from_bing = hang
        self.gen.total_budget = 6.0

        async def run():
            loop = asyncio.get_event_loop()
            start = loop.time()
            path = await self.gen.generate("Headline", "tech")
            return path, loop.time() - start

        path, elapsed = asyncio.run(run())
        self.assertIsNotNone(path)
        self.assertLess(elapsed, 30, "image generation ignored its time budget")

    def test_saved_file_is_a_readable_jpeg(self):
        path = asyncio.run(self.gen.generate("Headline here", "business"))
        from PIL import Image
        with Image.open(path) as im:
            self.assertEqual(im.size, (1280, 720))
            self.assertEqual(im.format, "JPEG")


class TestBingCookieAlert(unittest.TestCase):
    """An expired Bing cookie must ask for a replacement, not degrade silently."""

    def make(self):
        from modules.image_generator import ImageGenerator
        gen = ImageGenerator(output_dir=tempfile.mkdtemp(), bing_cookie="stale",
                             notification_manager=MagicMock(), db=MagicMock())
        gen.nm.send_notification = AsyncMock()
        gen.db.log_error = AsyncMock()
        # One attempt per call: the retry back-off is real time, and these
        # tests are about the alert, not the retry.
        gen.BING_ATTEMPTS = 1

        async def dead(*a, **k):
            raise RuntimeError("bing refused")
        gen._from_bing = dead
        return gen

    def test_one_failure_does_not_cry_wolf(self):
        """Bing throttles and times out; a single miss is not an expired cookie."""
        gen = self.make()
        asyncio.run(gen.generate("Headline", "tech"))
        gen.nm.send_notification.assert_not_awaited()
        self.assertFalse(gen.cookie_looks_dead)

    def test_repeated_failures_email_for_a_new_cookie(self):
        gen = self.make()
        for _ in range(gen.COOKIE_ALERT_AFTER):
            asyncio.run(gen.generate("Headline", "tech"))

        self.assertTrue(gen.cookie_looks_dead)
        gen.nm.send_notification.assert_awaited()
        kwargs = gen.nm.send_notification.call_args.kwargs
        self.assertTrue(kwargs["is_critical"])
        self.assertIn("_U", kwargs["message"], "the alert must name the cookie to replace")
        self.assertIn("BING_COOKIE", kwargs["message"])
        gen.db.log_error.assert_awaited()

    def test_alert_does_not_repeat_on_every_post(self):
        gen = self.make()
        for _ in range(gen.COOKIE_ALERT_AFTER + 4):
            asyncio.run(gen.generate("Headline", "tech"))
        self.assertEqual(gen.nm.send_notification.await_count, 1,
                         "a dead cookie must be reported once, not once per post")

    def test_recovery_is_reported_and_resets(self):
        from PIL import Image as _Image
        gen = self.make()
        for _ in range(gen.COOKIE_ALERT_AFTER):
            asyncio.run(gen.generate("Headline", "tech"))
        self.assertTrue(gen.cookie_looks_dead)

        async def alive(*a, **k):
            return _Image.new("RGB", (1280, 720), (10, 10, 10))
        gen._from_bing = alive
        asyncio.run(gen.generate("Headline", "tech"))

        self.assertFalse(gen.cookie_looks_dead)
        self.assertEqual(gen.consecutive_bing_failures, 0)
        self.assertEqual(gen.stats["bing"], 1)


class TestBroadcasterImageHonesty(unittest.TestCase):
    """The post record must say whether a photo was really attached."""

    def make(self):
        from modules.telegram_broadcaster import TelegramBroadcaster
        b = TelegramBroadcaster(api_id=1, api_hash="h", session_string="s",
                                channel_username="@c", db=MagicMock(), brain=None,
                                notification_manager=MagicMock())
        b._initialized = True
        b.client = MagicMock()
        b.db.log_post = AsyncMock()
        b.db.log_error = AsyncMock()
        b.nm.send_notification = AsyncMock()
        b.nm.notify_post_success = AsyncMock()
        b.get_subscriber_count = AsyncMock(return_value=0)
        return b

    def test_missing_file_is_not_logged_as_having_an_image(self):
        """
        Regression: the intended image path was written to the database on both
        branches, so a text-only post was indistinguishable from an illustrated
        one and the failure went unnoticed.
        """
        b = self.make()
        b._send_text_only = AsyncMock()
        asyncio.run(b.post({"telegram_text": "hello",
                            "image_path": os.path.join(tempfile.gettempdir(), "nope.jpg")}))

        kwargs = b.db.log_post.call_args.kwargs
        self.assertEqual(kwargs["image_path"], "")
        self.assertFalse(kwargs["metadata"]["has_image"])
        self.assertEqual(kwargs["metadata"]["image_status"], "file_missing")

    def test_posting_without_an_image_raises_an_alert(self):
        b = self.make()
        b._send_text_only = AsyncMock()
        asyncio.run(b.post({"telegram_text": "hello", "image_path": ""}))
        b.nm.send_notification.assert_awaited()
        self.assertTrue(b.nm.send_notification.call_args.kwargs["is_critical"])
        b.db.log_error.assert_awaited()

    def test_delivered_photo_is_recorded(self):
        b = self.make()
        fh = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        fh.write(b"x" * 5000)
        fh.close()
        try:
            b._send_photo_with_caption = AsyncMock(return_value=True)
            asyncio.run(b.post({"telegram_text": "hello", "image_path": fh.name}))
            kwargs = b.db.log_post.call_args.kwargs
            self.assertEqual(kwargs["image_path"], fh.name)
            self.assertTrue(kwargs["metadata"]["has_image"])
        finally:
            os.unlink(fh.name)

    def test_photo_failure_falls_back_to_text_rather_than_losing_the_post(self):
        b = self.make()
        fh = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        fh.write(b"x" * 5000)
        fh.close()
        try:
            b._send_photo_with_caption = AsyncMock(return_value=False)
            b._send_text_only = AsyncMock()
            ok = asyncio.run(b.post({"telegram_text": "hello", "image_path": fh.name}))
            self.assertTrue(ok, "a failed photo upload must not lose the post entirely")
            b._send_text_only.assert_awaited()
            self.assertEqual(
                b.db.log_post.call_args.kwargs["metadata"]["image_status"], "upload_failed")
        finally:
            os.unlink(fh.name)

    def test_caption_truncation_keeps_html_valid(self):
        """
        A naive slice can cut inside a tag or leave <b> unclosed, and Telegram
        rejects the whole message when it does — turning a long post into no
        post at all.
        """
        from modules.telegram_broadcaster import TelegramBroadcaster as TB
        for text, limit in (("<b>" + "word " * 500 + "</b>", 100),
                            ("<b><i>" + "y" * 400 + "</i></b>", 50),
                            ("A" * 80 + '<a href="https://x.example/p">link</a>', 90)):
            out = TB._truncate_html(text, limit)
            stack = []
            for m in re.finditer(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)[^>]*>", out):
                closing, tag = m.group(1), m.group(2).lower()
                if tag == "br":
                    continue
                if closing:
                    self.assertTrue(stack and stack.pop() == tag,
                                    f"unbalanced close </{tag}> in {out[:60]!r}")
                else:
                    stack.append(tag)
            self.assertFalse(stack, f"unclosed tags {stack} in {out[:60]!r}")
            self.assertEqual(out.count("<"), out.count(">"),
                             f"truncated mid-tag: {out[-40:]!r}")


class TestArticleAgentImages(unittest.TestCase):
    """The article agent must produce and host its own hero image."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_agent_accepts_an_image_generator(self):
        from modules.article_engine import ArticleAgent
        import inspect
        self.assertIn("image_gen", inspect.signature(ArticleAgent.__init__).parameters)

    def test_content_engine_hands_the_generator_to_the_agent(self):
        p = os.path.join(self.ROOT, "modules", "content_engine.py")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("image_gen=image_gen", src)

    def test_website_hero_is_not_the_outlets_photo(self):
        """
        Republishing a wire photograph on our own domain is a licensing problem
        that the source-credited Telegram post does not have.
        """
        p = os.path.join(self.ROOT, "modules", "fanout.py")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn('main_image_url=story.get("real_image_url")', src)

    def test_generated_hero_is_uploaded_not_left_on_disk(self):
        """Render wipes the disk on deploy, so a local path is not a hero URL."""
        p = os.path.join(self.ROOT, "modules", "article_engine.py")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("upload_image", src)

    def test_hero_generation_never_blocks_publishing(self):
        """An article still publishes when every image path fails."""
        from modules.article_engine import ArticleAgent

        gen = MagicMock()
        gen.generate = AsyncMock(side_effect=RuntimeError("image service down"))
        agent = ArticleAgent(ai_engine=MagicMock(), db=None,
                             site_name="Novi News", image_gen=gen)
        url = asyncio.run(agent._hero_image("A headline", "crypto"))
        self.assertEqual(url, "")


class TestFacebookImageAttachment(unittest.TestCase):
    """Facebook posts were going out with no picture at all."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def make(self):
        from modules.buffer_broadcaster import BufferBroadcaster
        b = BufferBroadcaster(access_token="t", db=MagicMock())
        b._connected = True
        b.db.log_social_post = AsyncMock()
        b.db.log_post = AsyncMock()
        b.db.log_error = AsyncMock()
        return b

    def _capture_post_input(self, broadcaster, image_url):
        captured = {}

        async def fake_gql(query, variables):
            captured.update(variables["i"])
            return {"createPost": {"__typename": "PostActionSuccess",
                                   "post": {"id": "1", "status": "queued"}}}
        broadcaster._gql = fake_gql
        asyncio.run(broadcaster.send(
            {"id": "c1", "name": "Page", "service": "facebook"},
            "caption text", image_url, "some-slug"))
        return captured

    def test_image_is_actually_attached(self):
        """
        Regression: the URL was computed and logged but `assets` was left
        empty, so every Facebook post published without its image.
        """
        b = self.make()
        captured = self._capture_post_input(b, "https://cdn.example.com/pic.jpg")
        assets = captured.get("assets")
        self.assertTrue(assets, "assets was empty — Facebook gets no image")
        self.assertEqual(assets[0]["image"]["url"], "https://cdn.example.com/pic.jpg")

    def test_facebook_still_requires_its_post_type(self):
        b = self.make()
        captured = self._capture_post_input(b, "https://cdn.example.com/pic.jpg")
        self.assertEqual(captured["metadata"]["facebook"]["type"], "post")

    def test_no_image_still_posts(self):
        """A missing picture must not block the post entirely."""
        b = self.make()
        captured = self._capture_post_input(b, "")
        self.assertEqual(captured.get("assets"), [])

    def test_local_paths_are_never_sent_as_urls(self):
        """Buffer fetches the URL itself, so a disk path would 404."""
        b = self.make()
        captured = self._capture_post_input(b, r"C:\assets\generated_images\post.jpg")
        self.assertEqual(captured.get("assets"), [])

    def test_the_picture_is_the_one_on_the_article(self):
        """
        Facebook and X show the hero the reader will see when they follow the
        link. It is already a public URL on our own storage, so there is
        nothing to choose between and nothing to re-host.
        """
        from modules.social_syndicator import SocialSyndicator
        buf = MagicMock()
        buf.ensure_channels = AsyncMock(
            return_value=[{"id": "c1", "service": "facebook"}])
        buf.send = AsyncMock(return_value=True)
        buf.last_error = ""
        syn = SocialSyndicator(buffer=buf, site_url="https://example.com",
                               services=["facebook"])
        asyncio.run(syn.syndicate({"slug": "s", "title": "A real headline here",
                                   "main_image_url": "https://ours/hero.jpg"}))
        self.assertEqual(buf.send.await_args.kwargs["image_url"],
                         "https://ours/hero.jpg")

    def test_content_engine_publishes_a_url_for_social_platforms(self):
        """Facebook and the website cannot upload the local file Telegram uses."""
        p = os.path.join(self.ROOT, "modules", "content_engine.py")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("upload_image(image_path)", src)
        self.assertIn('"image_url": image_url', src)


class TestEnglishOnly(unittest.TestCase):
    """Everything published must be in English — no Urdu or Roman Urdu."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_no_prompt_asks_for_urdu(self):
        """
        Regression: the morning brief asked for "90% English, 10% Urdu
        flavor", and went out to the channel in Roman Urdu.
        """
        offenders = []
        for folder in ("core", "modules"):
            base = os.path.join(self.ROOT, folder)
            for name in os.listdir(base):
                if not name.endswith(".py"):
                    continue
                with open(os.path.join(base, name), encoding="utf-8") as fh:
                    for lineno, line in enumerate(fh, 1):
                        low = line.lower()
                        if ("urdu" in low or "hinglish" in low) and "do not use" not in low:
                            offenders.append(f"{folder}/{name}:{lineno}: {line.strip()[:70]}")
        # reddit_broadcaster lists Urdu as a subreddit topic, not a prompt
        offenders = [o for o in offenders if "reddit_broadcaster" not in o]
        self.assertFalse(offenders, "prompts still request Urdu:\n" + "\n".join(offenders))

    def test_morning_brief_demands_english(self):
        p = os.path.join(self.ROOT, "modules", "content_engine.py")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        brief = src[src.index("produce_morning_brief"):]
        self.assertIn("100% professional, flawless English", brief)


class TestMorningBrief(unittest.TestCase):
    """The brief must be a brief — not the model thinking out loud."""

    # Verbatim from a live run: the free router picked a reasoning model and
    # returned its working out, which would have been posted to the channel.
    NARRATION = (
        "We need to produce morning brief with numbered list, 1-5 items, each "
        "1-2 lines, mention why it matters to readers in Pakistan. Use emojis. "
        'Must start with "Good morning! Here is your Novi News brief:" then a '
        "numbered list.\n\nWe need to cover top stories from given sources.\n\nWe need "
    )
    GOOD = (
        "Good morning! Here is your Novi News brief:\n\n"
        "1. Bitcoin ETFs post record inflows.\n"
        "2. Rupee steadies after IMF talks.\n\n"
        "Have a productive day! - Novi News"
    )

    def setUp(self):
        from modules.content_engine import ContentEngine
        self.CE = ContentEngine
        self.engine = ContentEngine.__new__(ContentEngine)
        self.engine.site_name = "Novi News"

    def test_reasoning_narration_is_rejected(self):
        self.assertIsNone(self.CE._extract_brief(self.NARRATION))

    def test_a_real_brief_survives(self):
        out = self.CE._extract_brief(self.GOOD)
        self.assertIsNotNone(out)
        self.assertTrue(out.lower().startswith("good morning"))

    def test_narration_before_the_brief_is_stripped(self):
        raw = ("The user wants a brief. Let me think about the format.\n\n"
               "Good morning! Here is your Novi News brief:\n\n"
               "1. Story one.\n2. Story two.\n\nHave a productive day!")
        out = self.CE._extract_brief(raw)
        self.assertTrue(out.lower().startswith("good morning"))
        self.assertNotIn("Let me think", out)

    def test_greeting_without_items_is_rejected(self):
        self.assertIsNone(
            self.CE._extract_brief("Good morning! Here is your brief: nothing today."))

    def test_empty_is_rejected(self):
        self.assertIsNone(self.CE._extract_brief(None))
        self.assertIsNone(self.CE._extract_brief(""))

    def test_composed_fallback_is_publishable(self):
        """When the model is unusable the brief is built from the stories."""
        text = self.engine._compose_brief([
            {"title": "Bitcoin ETF inflows hit record", "source": "CoinDesk"},
            {"title": "Rupee steadies after IMF talks", "source": "Dawn"},
        ])
        self.assertIn("Good morning", text)
        self.assertIn("Bitcoin ETF inflows hit record", text)
        self.assertIn("CoinDesk", text)
        self.assertIn("Have a productive day", text)
        self.assertGreaterEqual(len(re.findall(r"<b>\d+\.</b>", text)), 2)

    def test_composed_fallback_survives_missing_fields(self):
        text = self.engine._compose_brief([{"title": "Only a title"}, {}])
        self.assertIn("Only a title", text)
        self.assertIn("Good morning", text)


class TestSeoQualityGate(unittest.TestCase):
    """Thin metadata must never reach the site, whichever model produced it."""

    BODY = ("<p>Bitcoin and Ethereum moved sharply on Tuesday after the "
            "regulator confirmed new custody rules, with trading volume "
            "reaching its highest level this quarter and analysts pointing to "
            "renewed institutional demand across digital asset markets.</p>")

    def setUp(self):
        from modules.article_engine import ArticleAgent
        self.agent = ArticleAgent.__new__(ArticleAgent)
        self.agent.site_name = "Novi News"

    def repair(self, seo, title="Crypto markets swing after new custody rules",
               summary=""):
        return self.agent._repair_seo(dict(seo), title, summary, self.BODY, "crypto")

    def test_generic_meta_title_is_replaced_by_the_headline(self):
        """
        Regression: Groq returned "crypto market news and updates" as the
        title for a specific story — a section label, not a headline.
        """
        out = self.repair({"meta_title": "crypto market news and updates",
                           "meta_description": "x" * 120, "keywords": ["bitcoin price"]})
        self.assertEqual(out["meta_title"], "Crypto markets swing after new custody rules")

    def test_a_good_title_is_left_alone(self):
        good = "Crypto Markets Swing After New Custody Rules Confirmed"
        out = self.repair({"meta_title": good, "meta_description": "y" * 120,
                           "keywords": ["bitcoin price"]})
        self.assertEqual(out["meta_title"], good)

    def test_thin_description_is_rebuilt_from_the_article(self):
        """Regression: a 44-character description shipped to production."""
        out = self.repair({"meta_title": "Crypto markets swing after custody rules",
                           "meta_description": "latest crypto news",
                           "keywords": ["bitcoin price"]})
        self.assertGreaterEqual(len(out["meta_description"]), 90)
        self.assertLessEqual(len(out["meta_description"]), 160)
        self.assertIn("Bitcoin", out["meta_description"])

    def test_description_never_exceeds_google_limit(self):
        out = self.repair({"meta_title": "Crypto markets swing after custody rules",
                           "meta_description": "", "keywords": []})
        self.assertLessEqual(len(out["meta_description"]), 160)

    def test_generic_single_word_keywords_are_dropped(self):
        out = self.repair({"meta_title": "Crypto markets swing after custody rules",
                           "meta_description": "z" * 120,
                           "keywords": ["news", "market", "crypto", "update",
                                        "bitcoin price", "custody rules"]})
        self.assertNotIn("news", out["keywords"])
        self.assertNotIn("market", out["keywords"])
        self.assertIn("bitcoin price", out["keywords"])

    def test_keywords_are_topped_up_when_too_few_survive(self):
        out = self.repair({"meta_title": "Crypto markets swing after custody rules",
                           "meta_description": "z" * 120,
                           "keywords": ["news", "market"]})
        self.assertGreaterEqual(len(out["keywords"]), 4)

    def test_trim_stops_on_a_sentence_boundary(self):
        from modules.article_engine import ArticleAgent
        text = "First sentence here. Second sentence runs on and on and on and on."
        out = ArticleAgent._trim_to_sentence(text, 40)
        self.assertTrue(out.endswith(".") or out.endswith("…"))
        self.assertLessEqual(len(out), 41)


class TestArticleCategory(unittest.TestCase):
    """A crypto story must be filed under Crypto and illustrated as crypto."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_pipeline_key_maps_to_a_readable_section(self):
        from modules.article_engine import ArticleAgent as A
        self.assertEqual(A._DISPLAY_CATEGORIES["crypto"], "Crypto")
        self.assertEqual(A._DISPLAY_CATEGORIES["business_markets"], "Business")
        self.assertEqual(A._DISPLAY_CATEGORIES["world_news"], "World")

    def test_image_palette_matches_the_pipeline_key(self):
        from modules.article_engine import ArticleAgent as A
        self.assertEqual(A._image_category("crypto"), "crypto")
        self.assertEqual(A._image_category("tech_ai"), "tech_ai")

    def test_fanout_passes_the_chosen_category(self):
        """
        Regression: the article only saw the story's generic "News", so every
        article was filed under News and crypto stories got the world-news
        illustration.
        """
        p = os.path.join(self.ROOT, "modules", "fanout.py")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        # Articles are written on the website's own schedule now, so the
        # category reaches the agent from publish_scheduled_article rather
        # than from distribute(). The requirement is unchanged: the chosen
        # section must be passed, never the story's generic "News".
        self.assertIn("category=category", src)
        self.assertIn("self.pick_category()", src)


class TestToggleSafety(unittest.TestCase):
    """A misread question must never flip a module to the opposite state."""

    class Req:
        def __init__(self, body):
            self._body = body

        async def json(self):
            if self._body is None:
                raise ValueError("no body")
            return self._body

    def desired(self, body, current):
        from core.api_server import _desired_state
        return asyncio.run(_desired_state(self.Req(body), current))

    def test_turning_on_something_already_on_keeps_it_on(self):
        """
        Regression: "give me the report of the news agent" was read as a
        toggle, and because the endpoint blindly inverted, it switched the
        News Agent off.
        """
        self.assertTrue(self.desired({"active": True}, True))

    def test_turning_off_something_already_off_keeps_it_off(self):
        self.assertFalse(self.desired({"active": False}, False))

    def test_explicit_state_is_honoured(self):
        self.assertFalse(self.desired({"active": False}, True))
        self.assertTrue(self.desired({"active": True}, False))

    def test_string_forms_are_accepted(self):
        self.assertTrue(self.desired({"active": "true"}, False))
        self.assertTrue(self.desired({"active": "on"}, False))
        self.assertFalse(self.desired({"active": "false"}, True))

    def test_bare_toggle_still_flips(self):
        self.assertFalse(self.desired(None, True))
        self.assertTrue(self.desired(None, False))

    def test_every_toggle_endpoint_uses_the_helper(self):
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "core", "api_server.py")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        for flag in ("brain.news_module_active", "brain.website_module_active",
                     "brain.social_module_active", "brain.facebook_active",
                     "brain.twitter_active",
                     "sm._reply_active", "sm._scraping_active"):
            self.assertNotIn(f"{flag} = not {flag}", src,
                             f"{flag} still blindly inverts")
        self.assertGreaterEqual(src.count("await _desired_state("), 7)

    def test_the_social_switches_are_exposed(self):
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "core", "api_server.py")
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        for ep in ('"/api/social/toggle"', '"/api/social/{platform}/toggle"'):
            self.assertIn(ep, src)


class TestNoviIntentGuards(unittest.TestCase):
    """NOVI's prompt must not let a question trigger a toggle."""

    APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "novi-dashboard", "src", "App.jsx")

    def setUp(self):
        with open(self.APP, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_prompt_forbids_toggling_to_answer_questions(self):
        self.assertIn("NEVER TOGGLE ANYTHING TO ANSWER A QUESTION", self.src)

    def test_report_of_the_news_agent_is_called_out_explicitly(self):
        self.assertIn("report of the news agent", self.src.lower())

    def test_health_action_covers_single_module_questions(self):
        health = self.src[self.src.index('45. "health"'):]
        self.assertIn("READ-ONLY", health[:400])

    def test_dashboard_sends_the_desired_state(self):
        self.assertIn("response.desired", self.src)
        self.assertIn("JSON.stringify({ active: response.desired })", self.src)


class TestSourceImageExtraction(unittest.TestCase):
    """
    The publisher's own photo is the fallback when Bing cannot deliver, so it
    has to be found wherever a feed happens to put it. Only <enclosure> and
    <media:content> were checked, which is why Geo News and Dawn stories fell
    through to a drawn card.
    """

    def parse(self, item_xml: str):
        import xml.etree.ElementTree as ET
        from modules.news_scraper import NewsScraper
        ns = ('xmlns:media="http://search.yahoo.com/mrss/" '
              'xmlns:content="http://purl.org/rss/1.0/modules/content/"')
        root = ET.fromstring(f"<rss {ns}><channel>{item_xml}</channel></rss>")
        return NewsScraper._extract_image(root.find(".//item"))

    def test_enclosure(self):
        self.assertEqual(
            self.parse('<item><enclosure url="https://x.test/a.jpg" type="image/jpeg"/></item>'),
            "https://x.test/a.jpg")

    def test_media_content(self):
        self.assertEqual(
            self.parse('<item><media:content url="https://x.test/b.jpg" medium="image"/></item>'),
            "https://x.test/b.jpg")

    def test_media_content_without_medium_attribute(self):
        """Some feeds give only a url; the extension is the only signal."""
        self.assertEqual(
            self.parse('<item><media:content url="https://x.test/c.jpg"/></item>'),
            "https://x.test/c.jpg")

    def test_media_thumbnail(self):
        self.assertEqual(
            self.parse('<item><media:thumbnail url="https://x.test/d.jpg"/></item>'),
            "https://x.test/d.jpg")

    def test_img_inside_description(self):
        """The most common pattern of all, and it was ignored entirely."""
        self.assertEqual(
            self.parse('<item><description>'
                       '&lt;img src="https://x.test/e.jpg" /&gt;Story text'
                       '</description></item>'),
            "https://x.test/e.jpg")

    def test_img_inside_content_encoded(self):
        self.assertEqual(
            self.parse('<item><content:encoded>'
                       '&lt;p&gt;&lt;img src="https://x.test/f.jpg"&gt;&lt;/p&gt;'
                       '</content:encoded></item>'),
            "https://x.test/f.jpg")

    def test_protocol_relative_url_is_made_absolute(self):
        self.assertEqual(
            self.parse('<item><description>&lt;img src="//x.test/g.jpg"&gt;</description></item>'),
            "https://x.test/g.jpg")

    def test_non_image_enclosure_is_ignored(self):
        self.assertEqual(
            self.parse('<item><enclosure url="https://x.test/pod.mp3" type="audio/mpeg"/></item>'),
            "")

    def test_no_image_returns_empty(self):
        self.assertEqual(self.parse('<item><title>No art</title></item>'), "")

    def test_content_engine_asks_for_the_publisher_photo(self):
        """A feed with no artwork must still try the article page's og:image."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "modules", "content_engine.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("resolve_story_image", src)


class TestWebsiteImageRendering(unittest.TestCase):
    """Images must not be distorted by the page CSS."""

    CSS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "website", "app", "globals.css")

    def setUp(self):
        with open(self.CSS, encoding="utf-8") as fh:
            self.css = fh.read()

    def _block(self, selector: str) -> str:
        start = self.css.index(selector + " {")
        return self.css[start:self.css.index("}", start)]

    def test_hero_releases_the_intrinsic_height(self):
        """
        Regression: next/image emits width/height attributes; scaling width to
        100% without height:auto keeps the intrinsic height and squashes the
        picture — a 16:9 image rendered closer to 3:2.
        """
        hero = self._block(".hero")
        self.assertIn("height: auto", hero)
        self.assertIn("object-fit: cover", hero)

    def test_card_thumbnails_match_the_generated_ratio(self):
        card = self._block(".card-img")
        self.assertIn("height: auto", card)
        self.assertIn("aspect-ratio: 16 / 9", card)

    def test_no_hardcoded_foreign_domain_remains(self):
        root = os.path.dirname(self.CSS.rsplit("website", 1)[0])
        hits = []
        for folder, _, files in os.walk(os.path.join(root, "omni_channel_bot", "website", "app")):
            if "node_modules" in folder:
                continue
            for name in files:
                if name.endswith((".ts", ".tsx")):
                    with open(os.path.join(folder, name), encoding="utf-8") as fh:
                        if "novinews.pk" in fh.read():
                            hits.append(name)
        self.assertFalse(hits, f"hardcoded placeholder domain still in: {hits}")


class TestNarrationNeverPublished(unittest.TestCase):
    """
    27 of 191 published posts were the model thinking out loud. The samples
    below are verbatim from the database.
    """

    REAL_LEAKS = [
        "Here's a thinking process:\n\n1.  **Analyze the User's Request:**\n   - **Role:** Signal Editor",
        "We need to adapt the given text into a Facebook caption: keep every fact identical",
        "We need to determine if it's a locked VIP teaser. It has #XAG/USDT, Target Tuch 1",
        "Here in andells, let me analyze the raw signal to determine if it's a locked teaser",
        "Looking at the raw signal:\n#IMX/USDT\nLet me check the criteria:",
        "The instruction says extract core trading data: Coin pair, Direction, Leverage",
    ]

    REAL_GOOD = [
        "Bitcoin ETF inflows hit a record this week as institutional demand accelerated.",
        "#BTC/USDT LONG 20x\nEntry: 64000\nTP1: 66000\nSL: 62000\n\nPowered by @Novi_Network",
        "REJECT",
        "Good morning! Here is your Novi News brief:\n\n1. Markets rallied.\n2. Rupee steadied.",
        "Indian bond prices fell as the RBI hinted at rate hikes, rattling emerging markets.",
    ]

    def test_every_real_leak_is_detected(self):
        for sample in self.REAL_LEAKS:
            cleaned = AIEngine._strip_reasoning(sample)
            self.assertTrue(AIEngine.looks_like_narration(cleaned),
                            f"narration not detected: {sample[:60]!r}")

    def test_real_posts_are_not_flagged(self):
        for sample in self.REAL_GOOD:
            cleaned = AIEngine._strip_reasoning(sample)
            self.assertFalse(AIEngine.looks_like_narration(cleaned),
                             f"good copy wrongly rejected: {sample[:60]!r}")

    def test_think_tags_are_removed(self):
        out = AIEngine._strip_reasoning("<think>weighing it up</think>#BTC LONG 20x")
        self.assertEqual(out, "#BTC LONG 20x")

    def test_unclosed_think_tag_is_removed(self):
        """A token cut-off can leave the tag open."""
        self.assertEqual(AIEngine._strip_reasoning("answer here<think>cut off mid"), "answer here")

    def test_final_answer_label_keeps_only_the_answer(self):
        out = AIEngine._strip_reasoning("We need to do X.\n\nFinal answer: #ETH SHORT 10x")
        self.assertEqual(out, "#ETH SHORT 10x")

    def test_article_prose_using_these_words_later_is_safe(self):
        """"let me" mid-article must not trip the detector."""
        body = ("The central bank raised rates on Tuesday. " * 6 +
                "Officials said, let me be clear, that more may follow.")
        self.assertFalse(AIEngine.looks_like_narration(body))


class TestSignalValidator(unittest.TestCase):
    """The signal group took the worst of it - 19 of 80 posts were narration."""

    def setUp(self):
        from modules.signal_copier import SignalCopier
        self.valid = SignalCopier._is_valid_signal_output

    def test_real_signal_passes(self):
        self.assertTrue(self.valid(
            "#BTC/USDT LONG 20x\nEntry: 64000-64500\nTP1: 66000\nSL: 62000\n\n"
            "Powered by @Novi_Network"))

    def test_bare_reject_passes(self):
        self.assertTrue(self.valid("REJECT"))
        self.assertTrue(self.valid("  reject.  "))

    def test_narration_is_rejected(self):
        self.assertFalse(self.valid(
            "We need to determine if it's a locked VIP teaser. It has #XAG/USDT, Target Tuch 1"))

    def test_narrated_reject_is_rejected(self):
        """Explaining a rejection is not the same as returning REJECT."""
        self.assertFalse(self.valid(
            "Looking at this signal, there are lock emojis and no real numbers, "
            "so the correct action here is to REJECT it entirely."))

    def test_missing_footer_is_rejected(self):
        self.assertFalse(self.valid("#BTC/USDT LONG 20x Entry 64000 TP 66000"))

    def test_essay_length_is_rejected(self):
        self.assertFalse(self.valid("#BTC/USDT entry target " + "word " * 400 + "Powered by @x"))

    def test_empty_is_rejected(self):
        self.assertFalse(self.valid(""))
        self.assertFalse(self.valid(None))


class TestPostingSchedule(unittest.TestCase):
    """Six posts a day, and a failed post is retried rather than lost."""

    def test_six_slots_all_reachable(self):
        slots = BotBrain.SCHEDULE["post_slots"]
        start = BotBrain.SCHEDULE["sleep_start"]
        end = BotBrain.SCHEDULE["sleep_end"]
        self.assertEqual(len(slots), 6)
        unreachable = [s for s in slots if s["hour"] >= start or s["hour"] < end]
        self.assertFalse(unreachable,
                         f"slots inside the sleep window never fire: {unreachable}")

    def test_slots_are_spread_out(self):
        """Two posts 40 minutes apart read as a burst, not a schedule."""
        mins = sorted(s["hour"] * 60 + s["minute"] for s in BotBrain.SCHEDULE["post_slots"])
        gaps = [b - a for a, b in zip(mins, mins[1:])]
        self.assertTrue(all(g >= 120 for g in gaps), f"gaps too small: {gaps}")

    def test_failed_post_is_requeued(self):
        b = make_brain()
        self.assertTrue(b.queue_retry("regular_9_0", "regular", 0, "no real image"))
        self.assertEqual(len(b.pending_retries), 1)

    def test_retry_is_not_due_immediately(self):
        b = make_brain()
        b.queue_retry("regular_9_0", "regular", 0, "no real image")
        self.assertIsNone(b.due_retry(), "a retry must wait for its delay")

    def test_overdue_retry_is_returned_once(self):
        b = make_brain()
        past = (b._get_pkt_now() - timedelta(minutes=1)).isoformat()
        b.pending_retries = [{"key": "k", "type": "regular", "attempts": 1,
                              "due_at": past, "reason": "x"}]
        self.assertIsNotNone(b.due_retry())
        self.assertEqual(b.pending_retries, [], "a returned retry must leave the queue")
        self.assertIsNone(b.due_retry())

    def test_attempts_are_capped(self):
        """The slot is eventually published rather than retried forever."""
        b = make_brain()
        self.assertFalse(b.queue_retry("k", "regular", b.MAX_POST_ATTEMPTS, "still failing"))

    def test_retry_delay_is_thirty_minutes(self):
        self.assertEqual(BotBrain.RETRY_DELAY_MINUTES, 30)


class TestHotNewsRanking(unittest.TestCase):
    """Major world events must outrank filler."""

    def setUp(self):
        from modules.news_scraper import NewsScraper
        self.score = NewsScraper(db=None)._calculate_relevance_score

    def s(self, title):
        return self.score({"title": title, "summary": ""})

    def test_world_event_beats_routine_tech(self):
        self.assertGreater(self.s("Fed holds interest rates as inflation cools"),
                           self.s("New smartphone launched with better camera"))

    def test_roundups_are_penalised(self):
        """This exact headline produced a vague, contentless article."""
        self.assertLess(self.s("Here's what happened in crypto today"), 0)

    def test_filler_is_penalised(self):
        self.assertLess(self.s("Best deals on laptops this week"), 0)

    def test_real_event_still_scores_positive(self):
        self.assertGreater(self.s("Pakistan floods force mass evacuation in Sindh"), 0)


class TestLiveModelsExist(unittest.TestCase):
    """Groq retired the Llama family, which silently broke NOVI and articles."""

    def test_no_retired_llama_model_is_referenced(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dead = ("llama-3.1-8b-instant", "llama-3.3-70b-versatile", "llama3-8b-8192")
        hits = []
        for folder in ("core", "modules"):
            base = os.path.join(root, folder)
            for name in os.listdir(base):
                if not name.endswith(".py"):
                    continue
                with open(os.path.join(base, name), encoding="utf-8") as fh:
                    body = fh.read()
                # Strip comments: the fix is documented by naming the dead
                # model, and that explanation must not fail its own test.
                code = chr(10).join(line.split("#")[0]
                                    for line in body.splitlines())
                for model in dead:
                    if model in code:
                        hits.append(f"{folder}/{name}: {model}")
        self.assertFalse(hits, "retired Groq models still referenced: " + ", ".join(hits))


class TestTaskModelRouting(unittest.TestCase):
    """
    Short tasks must not run on the large model. Groq allows 8000 tokens a
    minute per key, so a 120b call for a six-line trading signal spends
    headroom the news post and article need.
    """

    def engine(self, provider="groq", default_model="openai/gpt-oss-120b"):
        e = AIEngine.__new__(AIEngine)
        e.provider = provider
        e.default_model = default_model
        return e

    def test_long_form_uses_the_quality_model(self):
        e = self.engine()
        self.assertEqual(e._model_for_task("synthesizer"), "openai/gpt-oss-120b")
        self.assertEqual(e._model_for_task("article"), "openai/gpt-oss-120b")

    def test_short_tasks_use_the_fast_model(self):
        e = self.engine()
        for task in ("signal_cleansing", "social_caption", "seo", "stealth"):
            self.assertEqual(e._model_for_task(task), "openai/gpt-oss-20b",
                             f"{task} should not run on the large model")

    def test_a_configured_model_does_not_pin_every_task(self):
        """
        Regression: setting NEWS_MODEL bypassed per-task routing entirely and
        put every task, including one-line signals, on the large model.
        """
        e = self.engine(default_model="openai/gpt-oss-120b")
        self.assertNotEqual(e._model_for_task("signal_cleansing"),
                            e._model_for_task("synthesizer"))

    def test_configured_model_still_wins_for_quality_tasks(self):
        e = self.engine(default_model="some/custom-model")
        self.assertEqual(e._model_for_task("synthesizer"), "some/custom-model")

    def test_unknown_task_defaults_to_quality(self):
        e = self.engine()
        self.assertEqual(e._model_for_task("something_new"), "openai/gpt-oss-120b")

    def test_unknown_provider_falls_back_to_the_configured_model(self):
        e = self.engine(provider="somethingelse", default_model="x/y")
        self.assertEqual(e._model_for_task("signal_cleansing"), "x/y")

    def test_every_task_in_the_models_map_has_a_tier(self):
        from core.ai_engine import MODELS, TASK_TIER
        missing = [t for t in MODELS if t not in TASK_TIER]
        self.assertFalse(missing, f"tasks with no tier assigned: {missing}")


class TestSeoMetadataQuality(unittest.TestCase):
    """
    Metadata is what Google shows. A truncated title, or a keyword list of
    "notches, best, since", is worse than none.
    """

    def setUp(self):
        from modules.article_engine import ArticleAgent
        self.A = ArticleAgent

    def test_keywords_are_search_phrases_not_stray_words(self):
        """Regression: the fallback returned single words split off the title."""
        kws = self.A._derive_keywords(
            "XRP Notches Best Week Since 2024 Election Pump on Bitcoin Short Squeeze",
            "Crypto")
        for junk in ("notches", "best", "since", "week"):
            self.assertNotIn(junk, kws, f"{junk!r} carries no search intent")
        self.assertTrue(any(" " in k for k in kws), "no multi-word phrase produced")

    def test_keywords_do_not_overlap_each_other(self):
        """A sliding window produced 'bitcoin short' and 'short squeeze' together."""
        kws = [k for k in self.A._derive_keywords(
            "Bitcoin short squeeze liquidations cascade", "Crypto") if " " in k]
        seen = set()
        for phrase in kws:
            words = set(phrase.split())
            self.assertFalse(words & seen, f"{phrase!r} repeats an earlier word")
            seen |= words

    def test_section_is_included(self):
        self.assertIn("crypto news",
                      self.A._derive_keywords("Bitcoin rallies hard", "Crypto"))

    def test_title_is_not_cut_mid_word(self):
        long_title = ("XRP Notches Best Week Since 2024 Election Pump on "
                      "Bitcoin Short Squeeze Rally")
        out = self.A._trim_to_sentence(long_title, 70)
        self.assertLessEqual(len(out), 71)
        self.assertFalse(out.rstrip("\u2026").endswith("Squeez"),
                         "title was cut in the middle of a word")

    def test_description_is_not_cut_mid_word(self):
        desc = ("XRP surged to its strongest weekly gain since the 2024 election, "
                "fueled by a Bitcoin short squeeze that triggered massive "
                "liquidations, underscoring its sensitivity to market stress.")
        out = self.A._trim_to_sentence(desc, 160)
        self.assertLessEqual(len(out), 161)
        self.assertFalse(out.rstrip("\u2026").endswith("sensiti"))

    def test_short_text_is_returned_untouched(self):
        self.assertEqual(self.A._trim_to_sentence("Short and fine.", 160),
                         "Short and fine.")


class TestPinCopywriter(unittest.TestCase):
    """Copy must never carry a price, and the disclosure is added in code."""

    def setUp(self):
        from pin_agent.content import PinCopywriter
        self.C = PinCopywriter

    def test_disclosure_is_always_appended(self):
        out = self.C._build_description({"description": "A useful gadget for small kitchens.",
                                         "hashtags": ["kitchen", "gadgets"]})
        self.assertIn("#ad", out, "the disclosure is required on every affiliate pin")

    def test_disclosure_survives_a_model_that_forgets_it(self):
        out = self.C._build_description({"description": "No tags, no disclosure here."})
        self.assertIn("#ad", out)

    def test_hashtags_are_normalised(self):
        out = self.C._build_description({"description": "Body copy that is long enough.",
                                         "hashtags": ["Kitchen Gadgets", "meal-prep", "meal-prep"]})
        self.assertIn("#kitchengadgets", out)
        self.assertEqual(out.count("#mealprep"), 1, "duplicate tags should collapse")

    def test_a_reply_containing_a_price_is_rejected(self):
        self.assertFalse(self.C._is_usable(
            '{"title": "Cheap gadget", "description": "Only $8.99 today"}'))

    def test_a_reply_without_the_expected_keys_is_rejected(self):
        self.assertFalse(self.C._is_usable('{"foo": "bar"}'))
        self.assertFalse(self.C._is_usable("We need to write a Pinterest title first"))

    def test_json_is_recovered_from_surrounding_prose(self):
        parsed = self.C._parse('Here you go:\n{"title": "A", "description": "B"}\nHope that helps')
        self.assertEqual(parsed["title"], "A")

    def test_shouted_titles_are_calmed(self):
        self.assertEqual(self.C._tidy_title("AMAZING KITCHEN GADGET!!"),
                         "Amazing Kitchen Gadget")

    def test_angle_rotates_away_from_recent_ones(self):
        from pin_agent.content import ANGLES
        writer = self.C.__new__(self.C)
        recent = list(ANGLES)[:-1]
        self.assertEqual(writer.pick_angle(recent), list(ANGLES)[-1])


class TestPinImageFormat(unittest.TestCase):
    """Pinterest ranks 2:3 vertical images; AliExpress photos are square."""

    def setUp(self):
        import tempfile
        from pin_agent.imaging import PinImageBuilder
        self.builder = PinImageBuilder(tempfile.mkdtemp(), brand="Novi")

    def test_output_is_pinterest_ratio(self):
        from pin_agent.imaging import PIN_HEIGHT, PIN_WIDTH
        img = self.builder.compose(None, "A pin title that is long enough", "time saver")
        self.assertEqual(img.size, (PIN_WIDTH, PIN_HEIGHT))
        self.assertAlmostEqual(PIN_HEIGHT / PIN_WIDTH, 1.5, places=2)

    def test_square_photo_is_not_distorted(self):
        """A square product photo must be cropped to fill, never stretched."""
        from PIL import Image as PILImage
        square = PILImage.new("RGB", (800, 800), (200, 150, 100))
        out = self.builder._cover(square, (1000, 1080))
        self.assertEqual(out.size, (1000, 1080))

    def test_renders_without_a_photo(self):
        img = self.builder.compose(None, "No photo available for this product", "")
        self.assertEqual(img.size, (1000, 1500))

    def test_renders_without_any_installed_font(self):
        """Render's container may ship no TTF files."""
        from PIL import ImageFont
        real = ImageFont.truetype

        def missing(font=None, size=10, *a, **kw):
            if isinstance(font, (str, bytes, os.PathLike)):
                raise OSError("no fonts installed")
            return real(font, size, *a, **kw)

        with patch("PIL.ImageFont.truetype", side_effect=missing):
            img = self.builder.compose(None, "Still renders with no fonts", "fallback")
        self.assertEqual(img.size, (1000, 1500))


class TestPinModuleIsOffByDefault(unittest.TestCase):
    """
    The agent spends an affiliate quota and posts to a public account, so it
    must never start on its own.
    """

    def test_defaults_to_off(self):
        self.assertFalse(make_brain().pin_module_active)

    def test_survives_a_restart(self):
        brain = make_brain()
        brain.pin_module_active = True
        self.assertIn("pin_module_active", brain.snapshot())
        self.assertTrue(brain.snapshot()["pin_module_active"])

    def test_pins_per_day_is_clamped_to_pinterest_guidance(self):
        """Pinterest recommends 5-15 a day; more reads as automation."""
        import os as _os
        from pin_agent.config import load_pin_config
        with patch.dict(_os.environ, {"PIN_MAX_PER_DAY": "50"}):
            self.assertLessEqual(load_pin_config().pins_per_day, 15)


class TestArticleQualityGate(unittest.TestCase):
    """
    The last check before an article is published.

    Every case here is a failure that actually reached the site or the feed:
    narration published as prose, a body cut off mid-sentence, a meta
    description sliced through a word.
    """

    def setUp(self):
        from modules.article_engine import ArticleAgent
        self.agent = ArticleAgent.__new__(ArticleAgent)
        body = "<p>" + ("The central bank raised rates on Tuesday. " * 40) + "</p>"
        self.good = {
            "title": "Central bank raises rates for the third time this year",
            "slug": "central-bank-raises-rates-third-time",
            "content": body,
            "word_count": 320,
            "category": "Business",
            "summary": "Rates rose again on Tuesday.",
            "meta_title": "Central bank raises rates for the third time",
            "meta_description": (
                "The central bank lifted its benchmark rate again on Tuesday, "
                "the third increase this year, as policymakers press on."),
            "seo_keywords": ["central bank", "interest rates", "monetary policy"],
        }

    def blocking(self, **overrides):
        return self.agent._quality_issues({**self.good, **overrides})[0]

    def fixable(self, **overrides):
        return self.agent._quality_issues({**self.good, **overrides})[1]

    def test_a_clean_article_passes_untouched(self):
        blocking, fixable = self.agent._quality_issues(self.good)
        self.assertEqual(blocking, [])
        self.assertEqual(fixable, [])

    def test_narration_is_blocked(self):
        body = "<p>Here is the article you requested. " + ("Filler. " * 80) + "</p>"
        self.assertTrue(self.blocking(content=body))

    def test_placeholder_text_is_blocked(self):
        body = "<p>Lorem ipsum dolor sit amet. " + ("More filler. " * 80) + "</p>"
        self.assertTrue(self.blocking(content=body))

    def test_doubled_word_is_blocked(self):
        body = "<p>The the bank raised rates. " + ("Prose follows. " * 80) + "</p>"
        self.assertTrue(any("doubled" in p for p in self.blocking(content=body)))

    def test_a_body_cut_off_mid_sentence_is_blocked(self):
        body = "<p>" + ("Prose continues. " * 80) + "and then it just</p>"
        self.assertTrue(any("mid-sentence" in p for p in self.blocking(content=body)))

    def test_unbalanced_markup_is_blocked(self):
        self.assertTrue(any("unbalanced" in p
                            for p in self.blocking(content="<p>" + ("Text here. " * 80))))

    def test_malformed_slug_is_blocked(self):
        self.assertTrue(any("slug" in p for p in self.blocking(slug="Bad_Slug!!")))

    def test_short_article_is_blocked(self):
        self.assertTrue(self.blocking(word_count=90))

    def test_hyphenated_compounds_are_not_doubled_words(self):
        # "on-chain" appears in most crypto copy, which is the largest section
        # on the site. Flagging "impact on on-chain activity" as a defect
        # deferred perfectly good articles.
        for text in ("the impact on on-chain activity rose",
                     "data on on-chain volumes climbed",
                     "they met in in-person talks"):
            body = f"<p>{text}. " + ("Prose continues here. " * 60) + "</p>"
            blocking = self.blocking(content=body)
            self.assertFalse([p for p in blocking if "doubled" in p],
                             f"false positive on: {text}")

    def test_genuine_doubled_words_are_still_caught(self):
        for text in ("a look at the the market",
                     "prices fell on on Tuesday",
                     "and and the results came in"):
            body = f"<p>{text}. " + ("Prose continues here. " * 60) + "</p>"
            self.assertTrue([p for p in self.blocking(content=body) if "doubled" in p],
                            f"missed a real defect: {text}")

    def test_seo_problems_are_fixable_not_blocking(self):
        # An over-long meta title is untidy. It is not a reason to spike the
        # piece, so it must never appear in the blocking list.
        overrides = dict(meta_title="x" * 120, meta_description="Short.",
                         seo_keywords=["one"])
        self.assertEqual(self.blocking(**overrides), [])
        self.assertTrue(self.fixable(**overrides))

    def test_repair_clears_every_fixable_problem(self):
        bad = {**self.good,
               "title": "CENTRAL BANK RAISES RATES AGAIN...",
               "meta_title": "x" * 120,
               "meta_description": "Short.",
               "summary": "",
               "seo_keywords": ["rates"]}
        _, fixable = self.agent._quality_issues(bad)
        self.assertTrue(fixable)

        repaired = self.agent._repair_record(bad, fixable)
        blocking_after, fixable_after = self.agent._quality_issues(repaired)
        self.assertEqual(fixable_after, [], f"repair left: {fixable_after}")
        self.assertEqual(blocking_after, [], "repair introduced a blocking fault")

    def test_repair_never_cuts_metadata_mid_word(self):
        bad = {**self.good, "meta_title": "Central bank raises interest rates "
                                          "for the third consecutive time this year"}
        repaired = self.agent._repair_record(bad, ["meta title too long"])
        self.assertLessEqual(len(repaired["meta_title"]), 70)
        # Whatever survives must end on a real boundary, not a half word.
        self.assertRegex(repaired["meta_title"], r"[\w.!?\u2026\)\"']$")


class TestSourcePhotoPreferredOverGenerated(unittest.TestCase):
    """
    The outlet's own photograph outranks a generated illustration.

    This was the other way round: Bing ran first and the real picture was
    only ever reached when Bing failed, which is backwards for reporting.
    """

    def test_generator_tries_the_story_photo_before_bing(self):
        import inspect
        from modules.image_generator import ImageGenerator
        src = inspect.getsource(ImageGenerator.generate)
        self.assertLess(src.index("story_image_url"), src.index("self.bing_cookie"),
                        "Bing must not be attempted before the story photograph")

    def test_three_generation_attempts(self):
        from modules.image_generator import ImageGenerator
        self.assertGreaterEqual(ImageGenerator.BING_ATTEMPTS, 3)

    def test_allow_card_false_is_honoured(self):
        import inspect
        from modules.image_generator import ImageGenerator
        self.assertIn("allow_card",
                      inspect.signature(ImageGenerator.generate).parameters)


class TestArticleDeferral(unittest.TestCase):
    """A story with no picture is held back, not published and not lost."""

    def test_fanout_retries_after_thirty_minutes(self):
        from modules.fanout import Fanout
        self.assertEqual(Fanout.ARTICLE_RETRY_MINUTES, 30)
        self.assertEqual(Fanout.MAX_ARTICLE_ATTEMPTS, 3)

    def test_a_deferred_story_is_queued(self):
        from modules.fanout import Fanout
        f = Fanout()
        f._defer_article({"title": "A story"}, {"image_url": "", "category": "World"},
                         reason="no hero image", attempts=0)
        self.assertEqual(f.deferred_count, 1)

    def test_it_gives_up_rather_than_looping_forever(self):
        from modules.fanout import Fanout
        f = Fanout()
        f._defer_article({"title": "A story"}, {}, reason="no image",
                         attempts=Fanout.MAX_ARTICLE_ATTEMPTS - 1)
        self.assertEqual(f.deferred_count, 0)

    def test_nothing_is_due_before_its_delay_elapses(self):
        from modules.fanout import Fanout
        f = Fanout()
        f._defer_article({"title": "A story"}, {}, reason="no image", attempts=0)
        published = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            f.retry_due_articles())
        self.assertEqual(published, 0)
        self.assertEqual(f.deferred_count, 1, "a not-yet-due story was dropped")


class TestScheduleReachesTheAudience(unittest.TestCase):
    """
    Slot times exist to reach readers, not to be tidy.

    Three of the previous six went out at midnight, 02:30 and 05:00 New York
    time, into an empty room. These pin the improvement so a later edit cannot
    quietly undo it.
    """

    @staticmethod
    def _in_zone(slot, offset_from_pkt):
        return (slot["hour"] - 5 + offset_from_pkt) % 24

    def setUp(self):
        from core.brain import BotBrain
        self.slots = BotBrain.SCHEDULE["post_slots"]
        self.sleep_start = BotBrain.SCHEDULE["sleep_start"]
        self.sleep_end = BotBrain.SCHEDULE["sleep_end"]

    def test_six_slots(self):
        self.assertEqual(len(self.slots), 6)

    def test_no_slot_falls_inside_the_sleep_window(self):
        # A slot inside the sleep window is skipped every single day, which is
        # how the channel used to see four posts when six were configured.
        for s in self.slots:
            self.assertFalse(s["hour"] >= self.sleep_start or s["hour"] < self.sleep_end,
                             f"{s['hour']:02d}:{s['minute']:02d} PKT is inside the "
                             f"sleep window and can never fire")

    def test_at_least_four_slots_reach_the_us(self):
        awake = [s for s in self.slots if 6 <= self._in_zone(s, -4) <= 14]
        self.assertGreaterEqual(len(awake), 4,
                                "fewer than four posts land in US waking hours")

    def test_every_slot_reaches_the_uk(self):
        awake = [s for s in self.slots if 7 <= self._in_zone(s, 1) <= 20]
        self.assertEqual(len(awake), len(self.slots))

    def test_slots_are_spread_not_clustered(self):
        minutes = sorted(s["hour"] * 60 + s["minute"] for s in self.slots)
        gaps = [b - a for a, b in zip(minutes, minutes[1:])]
        self.assertTrue(all(g >= 90 for g in gaps),
                        f"posts bunched too closely: {gaps}")


class TestCategoryMatchesTheAudience(unittest.TestCase):
    """
    Regional stories must not occupy the slots aimed at the West.

    A Pakistani domestic story is worth publishing; publishing it at 09:00
    New York spends the best slot of the day on the smallest audience.
    """

    def setUp(self):
        from modules.content_engine import ContentEngine
        self.engine = ContentEngine.__new__(ContentEngine)
        self.CE = ContentEngine

    def _draw(self, hour, n=400):
        from collections import Counter
        return Counter(self.engine._select_category(hour) for _ in range(n))

    def test_us_slots_never_carry_regional_stories(self):
        for hour in sorted(self.CE.US_FACING_HOURS_PKT):
            self.assertEqual(self._draw(hour)["pakistan"], 0,
                             f"a Pakistan story can reach the {hour}:00 PKT slot, "
                             f"which is US morning")

    def test_regional_stories_still_get_published(self):
        # Weighted down is fine; silenced is not. Pakistan is a section on the
        # site and must keep filling.
        early = self._draw(11)
        self.assertGreater(early["pakistan"], 0)

    def test_daily_volume_of_regional_coverage_is_preserved(self):
        # Moving stories between slots must not quietly cut how many are
        # published. Both mixes together should stay near the original 10%.
        from collections import Counter
        total = Counter()
        for hour in (11, 14, 16, 18, 20, 22):
            total += self._draw(hour, n=500)
        share = total["pakistan"] / sum(total.values())
        self.assertGreater(share, 0.05, f"regional coverage collapsed to {share:.1%}")
        self.assertLess(share, 0.20, f"regional coverage ballooned to {share:.1%}")

    def test_every_mix_only_names_real_categories(self):
        known = {c for c, _ in self.CE.CONTENT_MIX}
        for mix in (self.CE.GLOBAL_MIX, self.CE.REGIONAL_MIX):
            for cat, weight in mix:
                self.assertIn(cat, known, f"{cat} is not a known category")
                self.assertGreater(weight, 0)

    def test_no_hour_falls_back_to_the_original_mix(self):
        # The dashboard's manual "post now" passes no hour and must keep
        # working exactly as before.
        self.assertIn(self.engine._select_category(None),
                      {c for c, _ in self.CE.CONTENT_MIX})


class TestWebsiteHasItsOwnSchedule(unittest.TestCase):
    """
    The website publishes independently of Telegram.

    Articles used to be created inside the Telegram fan-out, so they
    inherited the sleep window -- 23:00-07:00 PKT, which is 14:00-22:00 in
    New York. The entire US afternoon and evening could never carry an
    article.
    """

    def setUp(self):
        from core.brain import BotBrain
        self.B = BotBrain
        self.slots = BotBrain.SCHEDULE["article_slots"]

    @staticmethod
    def _zone(slot, offset_from_pkt):
        return (slot["hour"] - 5 + offset_from_pkt) % 24

    def test_six_article_slots(self):
        self.assertEqual(len(self.slots), 6)

    def test_slots_run_through_the_sleep_window(self):
        # This is the whole point of the separate schedule. If no slot falls
        # inside it, the decoupling has quietly been undone.
        start = self.B.SCHEDULE["sleep_start"]
        end = self.B.SCHEDULE["sleep_end"]
        inside = [s for s in self.slots if s["hour"] >= start or s["hour"] < end]
        self.assertGreaterEqual(len(inside), 2,
                                "no article slot uses the hours the Telegram "
                                "schedule cannot reach")

    def test_us_afternoon_and_evening_are_covered(self):
        ny_hours = {self._zone(s, -4) for s in self.slots}
        self.assertTrue(any(14 <= h <= 20 for h in ny_hours),
                        f"nothing publishes during US afternoon/evening: {sorted(ny_hours)}")

    def test_most_slots_reach_the_us(self):
        awake = [s for s in self.slots if 6 <= self._zone(s, -4) <= 20]
        self.assertGreaterEqual(len(awake), 5)

    def test_article_slots_are_separate_from_telegram_slots(self):
        posts = {(s["hour"], s["minute"]) for s in self.B.SCHEDULE["post_slots"]}
        arts = {(s["hour"], s["minute"]) for s in self.slots}
        self.assertNotEqual(posts, arts,
                            "the two schedules are identical, so nothing was decoupled")

    def test_due_slot_ignores_the_sleep_window(self):
        # get_due_article_slot must not consult is_sleeping().
        import inspect
        src = inspect.getsource(self.B.get_due_article_slot)
        self.assertNotIn("is_sleeping", src)
        self.assertNotIn("can_post", src)


class TestNoDoublePublishing(unittest.TestCase):
    """
    A story must produce exactly one article.

    Two things can create one -- the website's scheduled run and the deferred
    retry -- and the Telegram fan-out must create none at all. If any two of
    those ever overlap, the same story is filed twice and _unique_slug quietly
    files the second as "...-2".
    """

    def _fanout(self, published_links=(), stories=None):
        from modules.fanout import Fanout

        class Brain:
            website_module_active = True

        class Agent:
            retry_after_minutes = 0
            last_skip_reason = ""
            def __init__(self): self.written = []
            async def generate_and_publish_article(self, story, main_image_url="", category=""):
                self.written.append(story["link"])
                return {"slug": "s" + str(len(self.written))}

        class Scraper:
            async def fetch_latest_news(self, category="all", force=False):
                return list(stories or [{"title": "T", "link": "https://x/new"}])

        seen = set(published_links)

        class DB:
            class client:
                @staticmethod
                def table(_):
                    class Q:
                        def select(self, *a): return self
                        def eq(self, c, v): self.v = v; return self
                        def limit(self, n): return self
                        def execute(self):
                            class R:
                                data = [{"slug": "old"}] if self.v in seen else []
                            return R()
                    return Q()

        agent = Agent()
        return Fanout(brain=Brain(), db=DB(), article_agent=agent,
                      scraper=Scraper(), pick_category=lambda: "crypto"), agent

    def test_the_telegram_fanout_creates_no_articles(self):
        # The single most important guard: if distribute() writes articles
        # again, every story is published twice.
        import inspect
        from modules.fanout import Fanout
        src = inspect.getsource(Fanout.distribute)
        self.assertNotIn("generate_and_publish_article", src)

    def test_a_story_already_written_up_is_skipped(self):
        f, agent = self._fanout(
            published_links={"https://x/old"},
            stories=[{"title": "Old", "link": "https://x/old"},
                     {"title": "New", "link": "https://x/new"}])
        asyncio.run(f.publish_scheduled_article())
        self.assertEqual(agent.written, ["https://x/new"])

    def test_nothing_is_written_when_every_story_is_known(self):
        f, agent = self._fanout(
            published_links={"https://x/a"},
            stories=[{"title": "A", "link": "https://x/a"}])
        self.assertIsNone(asyncio.run(f.publish_scheduled_article()))
        self.assertEqual(agent.written, [])

    def test_a_retry_drops_if_the_story_was_published_meanwhile(self):
        # The scheduled run picks stories independently, so it can easily
        # publish the very story sitting in the retry queue.
        f, agent = self._fanout(published_links={"https://x/queued"})
        f._deferred = [{
            "story": {"title": "Queued", "link": "https://x/queued"},
            "image_url": "", "category": "crypto",
            "due": datetime.now(timezone.utc) - timedelta(minutes=1),
            "attempts": 1, "reason": "no hero image",
        }]
        published = asyncio.run(f.retry_due_articles())
        self.assertEqual(published, 0)
        self.assertEqual(agent.written, [], "the retry wrote a duplicate")

    def test_a_retry_still_runs_when_the_story_is_genuinely_unpublished(self):
        f, agent = self._fanout(published_links=set())
        f._deferred = [{
            "story": {"title": "Queued", "link": "https://x/queued"},
            "image_url": "", "category": "crypto",
            "due": datetime.now(timezone.utc) - timedelta(minutes=1),
            "attempts": 1, "reason": "no hero image",
        }]
        self.assertEqual(asyncio.run(f.retry_due_articles()), 1)
        self.assertEqual(agent.written, ["https://x/queued"])

    def test_deduplication_uses_the_source_url_not_the_headline(self):
        # The agent rewords headlines, so a title match would miss duplicates.
        import inspect
        from modules.fanout import Fanout
        src = inspect.getsource(Fanout._already_published)
        self.assertIn("source_url", src)
        self.assertNotIn('"title"', src)


class TestWebsiteRunIsNotGatedBySleep(unittest.TestCase):
    """
    The website block must sit ABOVE the sleep check in the main loop.

    This is the bug that made the whole decoupling useless in production. The
    schedule was right, get_due_article_slot correctly ignored the sleep
    window, and there was a test asserting exactly that -- but the CALLER sat
    below `if brain.is_sleep_time(): continue`, so during 23:00-07:00 PKT the
    loop restarted before ever reaching it. The 01:00 and 04:00 slots, which
    are the entire reason the website has its own schedule, could never run.

    Testing the brain method was testing the wrong layer. This tests the order
    of the loop itself.
    """

    def setUp(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "main.py"), encoding="utf-8") as fh:
            self.src = fh.read()

    def _index(self, needle):
        i = self.src.find(needle)
        self.assertNotEqual(i, -1, f"could not find {needle!r} in main.py")
        return i

    def test_article_run_comes_before_the_sleep_gate(self):
        article = self._index("publish_scheduled_article()")
        sleep_gate = self._index("if brain.is_sleep_time():")
        self.assertLess(article, sleep_gate,
                        "the website's publishing run sits below the sleep "
                        "gate, so it cannot fire during the sleep window -- "
                        "which is where two of its six slots live")

    def test_deferred_retry_comes_before_the_sleep_gate(self):
        retry = self._index("retry_due_articles()")
        sleep_gate = self._index("if brain.is_sleep_time():")
        self.assertLess(retry, sleep_gate,
                        "a deferred article cannot be retried during the "
                        "sleep window")

    def test_the_telegram_slots_stay_below_the_sleep_gate(self):
        # The channel must still sleep. Only the website is exempt.
        sleep_gate = self._index("if brain.is_sleep_time():")
        telegram = self._index("brain.get_next_post_slot()")
        self.assertLess(sleep_gate, telegram,
                        "the Telegram schedule escaped the sleep window")

    def test_master_kill_still_stops_the_website(self):
        # Exempt from sleep is not exempt from the kill switch.
        kill = self._index("if brain.master_kill:")
        article = self._index("publish_scheduled_article()")
        self.assertLess(kill, article)


class TestStockPhotoLicensing(unittest.TestCase):
    """
    Explainers use real photographs, and the licence has to be respected.

    The pipeline crops every image to 1280x720, which is a derivative work,
    and the site carries affiliate links, which makes it commercial. Both
    rule out whole licence classes.
    """

    def setUp(self):
        from modules.stock_photos import StockPhotoFinder
        self.F = StockPhotoFinder
        self.f = StockPhotoFinder()

    def _item(self, **kw):
        base = {"url": "https://x/p.jpg", "license": "cc0",
                "width": 1600, "height": 900, "title": "bitcoin coin"}
        base.update(kw)
        return base

    def test_no_derivatives_licences_are_refused(self):
        # We crop to 16:9. That is a derivative.
        self.assertFalse(self.F._usable(self._item(license="by-nd")))
        self.assertFalse(self.F._usable(self._item(license="by-nc-nd")))

    def test_non_commercial_licences_are_refused(self):
        self.assertFalse(self.F._usable(self._item(license="by-nc")))

    def test_permitted_licences_pass(self):
        for lic in ("cc0", "pdm", "by", "by-sa"):
            self.assertTrue(self.F._usable(self._item(license=lic)), lic)

    def test_small_images_are_refused(self):
        # A thumbnail upscaled into a hero looks worse than no picture.
        self.assertFalse(self.F._usable(self._item(width=320, height=200)))

    def test_public_domain_needs_no_credit(self):
        for lic in ("cc0", "pdm"):
            self.assertEqual(self.F.credit_for(self._item(license=lic)), "")

    def test_attributed_licences_produce_a_credit(self):
        credit = self.F.credit_for(self._item(license="by-sa", creator="A Person",
                                              source="wikimedia"))
        self.assertIn("A Person", credit)
        self.assertIn("BY-SA", credit)

    def test_irrelevant_results_are_rejected(self):
        # Openverse answered "financial report documents" with an Egyptian
        # papyrus. Technically a document; useless on the page.
        papyrus = self._item(title="Abusir papyrus - Pharaoh exhibit")
        self.assertFalse(self.F._relevant(papyrus, "financial report documents"))

    def test_relevant_results_are_kept(self):
        self.assertTrue(self.F._relevant(self._item(title="Bitcoin coins on a desk"),
                                         "bitcoin cryptocurrency"))
        self.assertTrue(self.F._relevant(
            self._item(title="Untitled", tags=[{"name": "solar"}, {"name": "panel"}]),
            "solar panels rooftop"))


class TestEvergreenDesk(unittest.TestCase):
    """The explainer desk: what it writes and what it refuses to repeat."""

    def setUp(self):
        from modules.evergreen import EvergreenDesk, TOPIC_BANK
        self.D = EvergreenDesk
        self.bank = TOPIC_BANK

    def test_every_topic_is_complete(self):
        for t in self.bank:
            for field in ("category", "title", "angle", "photo"):
                self.assertTrue(t.get(field), f"{t.get('title')} is missing {field}")

    def test_topics_are_unique(self):
        titles = [t["title"] for t in self.bank]
        self.assertEqual(len(titles), len(set(titles)))

    def test_topic_keys_are_stable_and_distinct(self):
        keys = {self.D._topic_key(t["title"]) for t in self.bank}
        self.assertEqual(len(keys), len(self.bank))
        self.assertEqual(self.D._topic_key("How Bitcoin halving affects the price"),
                         self.D._topic_key("How Bitcoin halving affects the price"))

    def test_topic_key_is_namespaced(self):
        # Stored in source_url, which also holds real article URLs.
        self.assertTrue(self.D._topic_key("Anything").startswith("evergreen:"))

    def test_enough_topics_for_the_schedule(self):
        from core.brain import BotBrain
        per_day = len(BotBrain.SCHEDULE["evergreen_slots"])
        self.assertGreaterEqual(len(self.bank) / per_day, 30,
                                "fewer than thirty days of explainers in the bank")

    def test_photo_queries_are_concrete_nouns(self):
        # A photo archive can answer "office workspace" and cannot answer
        # "productivity". Abstract queries are what drove the hit rate down.
        abstract = {"productivity", "innovation", "strategy", "growth",
                    "success", "future", "digital", "modern"}
        for t in self.bank:
            words = set(t["photo"].lower().split())
            self.assertFalse(words <= abstract,
                             f"{t['photo']!r} is too abstract to photograph")

    def test_the_desk_replenishes_rather_than_stopping(self):
        # 77 curated topics is 38 days. A pipeline that silently halts after
        # five weeks is not a pipeline.
        import inspect
        src = inspect.getsource(self.D.next_topic)
        self.assertIn("_invent_topic", src)

    def test_low_stock_threshold_is_reached_before_empty(self):
        self.assertGreater(self.D.LOW_STOCK, 0)
        self.assertLess(self.D.LOW_STOCK, len(self.bank))

    def test_the_bank_covers_every_section(self):
        sections = {t["category"] for t in self.bank}
        for required in ("crypto", "pakistan", "business_markets", "tech_ai"):
            self.assertIn(required, sections)


# ═══════════════════════════════════════════════════════════════
#  SOCIAL SYNDICATION — Facebook and X
# ═══════════════════════════════════════════════════════════════
class TestSocialSyndicator(unittest.TestCase):
    """
    Facebook and X announce every article the moment it publishes.

    Three things have to hold, and each has already gone wrong once in some
    form: every post carries a link, X posts fit in 280 characters, and the
    trigger is the ARTICLE rather than the Telegram schedule.
    """

    ARTICLE = {
        "slug": "bitcoin-halving-explained",
        "title": "How the Bitcoin halving affects the price, explained",
        "meta_description": ("The halving cuts new supply in half roughly every "
                             "four years. Here is the mechanism, the historical "
                             "record, and why past cycles are not a promise."),
        "summary": "A plain-English look at what the halving does to supply.",
        "seo_keywords": ["bitcoin halving", "crypto supply", "the price of bitcoin"],
        "main_image_url": "https://cdn.example/hero.jpg",
    }

    def _syn(self, **kw):
        from modules.social_syndicator import SocialSyndicator
        buf = kw.pop("buffer", None)
        if buf is None:
            buf = MagicMock()
            buf.channels_for = MagicMock(return_value=[{"id": "c1", "service": "facebook"}])
            buf.ensure_channels = AsyncMock(
                return_value=[{"id": "c1", "service": "facebook"}])
            buf.send = AsyncMock(return_value=True)
            buf.last_error = ""
        syn = SocialSyndicator(
            buffer=buf,
            site_url=kw.pop("site_url", "https://pressvane.com"),
            services=kw.pop("services", ["facebook", "twitter"]),
            **kw)
        syn._buffer_mock = buf
        return syn

    # ── the link is the entire point ─────────────────────────────

    def test_every_post_carries_the_article_link(self):
        syn = self._syn()
        link = syn.article_link(self.ARTICLE["slug"])
        self.assertEqual(link, "https://pressvane.com/bitcoin-halving-explained")
        for service in ("facebook", "twitter"):
            self.assertIn(link, syn.caption_for(service, self.ARTICLE, link))

    def test_nothing_is_posted_when_there_is_no_link_to_give(self):
        """A post with no link is worse than no post: it spends the reach and
        sends nobody to the site."""
        syn = self._syn(site_url="")
        results = asyncio.run(syn.syndicate(self.ARTICLE))
        self.assertEqual(results, {})
        syn._buffer_mock.send.assert_not_awaited()

    def test_an_article_with_no_slug_is_not_posted(self):
        syn = self._syn()
        self.assertEqual(asyncio.run(syn.syndicate({"title": "T"})), {})
        syn._buffer_mock.send.assert_not_awaited()

    # ── X's 280 characters are hard ──────────────────────────────

    def test_x_caption_fits_280_characters(self):
        syn = self._syn()
        link = syn.article_link(self.ARTICLE["slug"])
        text = syn.x_caption(self.ARTICLE, link)
        self.assertLessEqual(syn.x_length(text, link), syn.X_MAX_CHARS)
        self.assertIn(link, text)

    def test_x_caption_fits_even_with_an_enormous_headline(self):
        """Buffer rejects an over-length X post outright, so the trim has to
        survive input nobody expected."""
        syn = self._syn()
        link = syn.article_link("s")
        article = dict(self.ARTICLE, title="Bitcoin " * 80,
                       seo_keywords=["a very long keyword phrase indeed here"] * 6)
        text = syn.x_caption(article, link)
        self.assertLessEqual(syn.x_length(text, link), syn.X_MAX_CHARS)
        self.assertIn(link, text)

    def test_x_prices_the_link_at_23_characters(self):
        """X rewrites every URL through t.co, so a long slug costs no more
        than a short one. Measuring the raw string wastes the budget."""
        syn = self._syn()
        short = syn.x_length("hello https://a.co/x", "https://a.co/x")
        long_link = "https://pressvane.com/" + "a" * 200
        long_ = syn.x_length(f"hello {long_link}", long_link)
        self.assertEqual(short, long_)

    def test_a_short_opening_sentence_still_counts_as_context(self):
        """
        Regression: the sentence-boundary guard was a fraction of the budget,
        so a generous 172-character budget rejected a good 78-character
        opening sentence and the tweet went out as a bare headline.
        """
        syn = self._syn()
        long_summary = ("The halving cuts the reward for mining a block in half "
                        "roughly every four years. This explains the mechanism, "
                        "what happened in each past cycle, and why that record "
                        "is not a promise.")
        article = dict(self.ARTICLE, summary=long_summary)
        link = syn.article_link(article["slug"])
        text = syn.x_caption(article, link)
        self.assertIn("every four years.", text)
        self.assertNotIn("This explains", text, "only the first whole sentence")
        self.assertLessEqual(syn.x_length(text, link), syn.X_MAX_CHARS)

    def test_a_stub_is_still_rejected(self):
        """A four-word first sentence is not a summary."""
        syn = self._syn()
        article = dict(self.ARTICLE, meta_description="", summary=(
            "It fell. " + "Then a much longer clause that runs past the budget "
            "and keeps going well beyond it " * 4))
        self.assertEqual(syn._blurb(article, 120), "")

    def test_x_caption_keeps_the_headline_intact_when_it_fits(self):
        syn = self._syn()
        link = syn.article_link(self.ARTICLE["slug"])
        self.assertIn(self.ARTICLE["title"], syn.x_caption(self.ARTICLE, link))

    # ── Facebook ─────────────────────────────────────────────────

    def test_a_blurb_cut_off_mid_sentence_is_never_published(self):
        """
        Regression: meta_description is trimmed to 160 characters for the
        search snippet and almost always stops mid-clause ("...and token
        age-spent, helping"). Invisible in a search result, glaring in a
        Facebook post. summary is written whole, so it goes first.
        """
        syn = self._syn()
        link = "https://pressvane.com/s"
        cut = {"slug": "s", "title": "A headline that is long enough",
               "meta_description": "Bitcoin fell before rebounding above, showing",
               "summary": "Bitcoin fell to $78,630 before rebounding above $79,000."}
        self.assertIn("$79,000.", syn.facebook_caption(cut, link))
        self.assertNotIn("showing", syn.facebook_caption(cut, link))

        # Nothing whole to say: the post is headline and link, not a fragment.
        only_fragment = dict(cut, summary="")
        text = syn.facebook_caption(only_fragment, link)
        self.assertNotIn("showing", text)
        self.assertIn(link, text)

    def test_facebook_caption_uses_the_article_not_a_model_rewrite(self):
        syn = self._syn()
        link = syn.article_link(self.ARTICLE["slug"])
        text = syn.facebook_caption(self.ARTICLE, link)
        self.assertIn(self.ARTICLE["title"], text)
        self.assertIn("what the halving does to supply", text)
        self.assertIn(link, text)

    def test_facebook_caption_stays_under_the_feed_limit(self):
        syn = self._syn()
        article = dict(self.ARTICLE, summary="word " * 5000,
                       meta_description="word " * 5000)
        text = syn.facebook_caption(article, "https://pressvane.com/s")
        self.assertLessEqual(len(text), syn.FACEBOOK_MAX_CHARS)

    # ── hashtags ─────────────────────────────────────────────────

    def test_hashtags_are_built_from_the_seo_keywords(self):
        from modules.social_syndicator import SocialSyndicator as S
        self.assertEqual(S._hashtag("bitcoin halving"), "#BitcoinHalving")
        self.assertEqual(S._hashtag("the price of bitcoin"), "#PriceOfBitcoin")

    def test_a_compound_term_survives_intact(self):
        """
        Regression: splitting on the hyphen left "on" looking like a stop word
        standing alone, and "on-chain analysis" published as #ChainAnalysis --
        a different subject. Crypto is the largest section on the site.
        """
        from modules.social_syndicator import SocialSyndicator as S
        self.assertEqual(S._hashtag("on-chain analysis"), "#OnChainAnalysis")
        self.assertEqual(S._hashtag("risk-on sentiment"), "#RiskOnSentiment")
        self.assertEqual(S._hashtag("trump mail-in challenge"),
                         "#TrumpMailInChallenge")

    def test_the_ai_writes_dashes_that_are_not_hyphens(self):
        """The model emits U+2011 and friends; they must not split a term."""
        from modules.social_syndicator import SocialSyndicator as S
        self.assertEqual(S._hashtag("token age‑spent analysis"),
                         "#TokenAgeSpentAnalysis")
        self.assertEqual(S._hashtag("on‑chain analysis"), "#OnChainAnalysis")

    def test_useless_hashtags_are_dropped(self):
        from modules.social_syndicator import SocialSyndicator as S
        self.assertEqual(S._hashtag("the"), "")
        self.assertEqual(S._hashtag("2026"), "")
        self.assertEqual(S._hashtag("!!!"), "")
        self.assertEqual(S._hashtag("a phrase far too long to be a hashtag"), "")
        self.assertEqual(S._hashtag("financial metrics interpretation"), "",
                         "31 characters is not a hashtag")

    def test_hashtags_are_deduplicated_and_capped(self):
        from modules.social_syndicator import SocialSyndicator as S
        tags = S.hashtags(["bitcoin halving", "Bitcoin Halving", "crypto"], 2)
        self.assertEqual(tags, ["#BitcoinHalving", "#Crypto"])

    # ── volume ───────────────────────────────────────────────────

    def test_full_cap_shares_every_article(self):
        syn = self._syn()
        self.assertEqual(syn.daily_cap("facebook"), 8)
        self.assertEqual(len(syn.SLOT_PRIORITY), 8)

    def test_a_new_account_ramps_up_on_its_own(self):
        today = (datetime.now(timezone.utc) + PKT).date()
        for days, expected in ((0, 3), (4, 3), (5, 5), (10, 5),
                               (11, 6), (17, 6), (18, 8), (400, 8)):
            syn = self._syn(start_date=str(today - timedelta(days=days)))
            self.assertEqual(syn.daily_cap("facebook"), expected,
                             f"day {days + 1} of the account should allow {expected}")

    def test_the_ramp_only_ever_climbs(self):
        today = (datetime.now(timezone.utc) + PKT).date()
        caps = [self._syn(start_date=str(today - timedelta(days=d))).daily_cap("facebook")
                for d in range(0, 40)]
        self.assertEqual(caps, sorted(caps), "the daily limit must never drop")
        self.assertEqual(caps[-1], 8, "and must reach the full cap")

    def test_day_one_stamps_itself_on_the_first_post(self):
        """
        The ramp needs to know the account's age, and asking someone to set a
        date is a step that gets forgotten -- which means eight posts a day
        out of a page with no history, the exact thing the ramp prevents.
        """
        brain = make_brain()
        brain.social_module_active = True
        self.assertEqual(brain.social_started_on, "")

        syn = self._syn(services=["facebook"], brain=brain)
        syn._slot_is_allowed = lambda *a: True
        self.assertIsNone(syn.days_live())

        asyncio.run(syn.syndicate(self.ARTICLE))
        today = (datetime.now(timezone.utc) + PKT).date().isoformat()
        self.assertEqual(brain.social_started_on, today)
        self.assertEqual(syn.days_live(), 0)
        self.assertEqual(syn.daily_cap("facebook"), 3, "day one starts small")

    def test_day_one_survives_a_redeploy(self):
        brain = make_brain()
        brain.social_started_on = "2026-08-01"
        self.assertEqual(brain.snapshot()["social_started_on"], "2026-08-01")

        restored = make_brain()
        restored.db = MagicMock()
        restored.db.load_state = AsyncMock(return_value=brain.snapshot())
        asyncio.run(restored.restore_state())
        self.assertEqual(restored.social_started_on, "2026-08-01")

    def test_a_failed_post_does_not_start_the_clock(self):
        brain = make_brain()
        brain.social_module_active = True
        buf = MagicMock()
        buf.ensure_channels = AsyncMock(
            return_value=[{"id": "c1", "service": "facebook"}])
        buf.send = AsyncMock(return_value=False)
        buf.last_error = "rejected"
        syn = self._syn(buffer=buf, services=["facebook"], brain=brain)
        syn._slot_is_allowed = lambda *a: True
        asyncio.run(syn.syndicate(self.ARTICLE))
        self.assertEqual(brain.social_started_on, "")

    def test_a_configured_date_overrides_the_stamp(self):
        brain = make_brain()
        brain.social_started_on = "2020-01-01"
        today = (datetime.now(timezone.utc) + PKT).date()
        syn = self._syn(brain=brain, start_date=str(today - timedelta(days=2)))
        self.assertEqual(syn.days_live(), 2)

    def test_no_start_date_anywhere_means_no_ramp(self):
        self.assertIsNone(self._syn().days_live())
        self.assertEqual(self._syn(start_date="not-a-date").daily_cap("twitter"), 8)

    def test_a_configured_cap_is_never_exceeded_by_the_ramp(self):
        today = (datetime.now(timezone.utc) + PKT).date()
        syn = self._syn(caps={"facebook": 3}, start_date=str(today - timedelta(days=400)))
        self.assertEqual(syn.daily_cap("facebook"), 3)

    def test_the_daily_cap_stops_further_posts(self):
        syn = self._syn(services=["facebook"], caps={"facebook": 1})
        syn._slot_is_allowed = lambda *a: True   # isolate the counter
        asyncio.run(syn.syndicate(self.ARTICLE))
        asyncio.run(syn.syndicate(dict(self.ARTICLE, slug="second")))
        self.assertEqual(syn._buffer_mock.send.await_count, 1)
        self.assertEqual(syn.sent_today["facebook"], 1)

    def test_counters_reset_when_the_pkt_date_changes(self):
        syn = self._syn(services=["facebook"], caps={"facebook": 1})
        syn._slot_is_allowed = lambda *a: True   # isolate the counter
        asyncio.run(syn.syndicate(self.ARTICLE))
        self.assertEqual(syn.sent_today["facebook"], 1)
        syn._counter_day = syn._counter_day - timedelta(days=1)   # yesterday
        asyncio.run(syn.syndicate(dict(self.ARTICLE, slug="second")))
        self.assertEqual(syn.sent_today["facebook"], 1)
        self.assertEqual(syn._buffer_mock.send.await_count, 2)

    def test_a_rejected_post_does_not_consume_the_day_allowance(self):
        buf = MagicMock()
        buf.ensure_channels = AsyncMock(
            return_value=[{"id": "c1", "service": "facebook"}])
        buf.send = AsyncMock(return_value=False)
        buf.last_error = "rate limited"
        syn = self._syn(buffer=buf, services=["facebook"])
        asyncio.run(syn.syndicate(self.ARTICLE))
        self.assertEqual(syn.sent_today["facebook"], 0)

    # ── which articles survive the warm-up cap ───────────────────

    def test_the_warm_up_keeps_the_slots_the_audience_is_awake_for(self):
        """
        Not "the first four of the day". The PKT day starts at midnight while
        the readers are in London and New York, so first-come would hand every
        share to the small hours.
        """
        syn = self._syn()
        top4 = syn.SLOT_PRIORITY[:4]
        self.assertIn((18, 0), top4, "18:00 PKT is 09:00 in New York")
        self.assertIn((21, 0), top4, "21:00 PKT is 12:00 in New York")
        self.assertNotIn((13, 0), top4, "13:00 PKT is 04:00 in New York")
        self.assertNotIn((11, 30), top4, "11:30 PKT is 02:30 in New York")

    def test_every_priority_slot_is_a_slot_the_website_actually_publishes_in(self):
        """
        A time listed here that the brain does not publish at would silently
        mute a platform, and the mismatch would be invisible until someone
        counted posts a week later.
        """
        from modules.social_syndicator import SocialSyndicator as S
        scheduled = {(s["hour"], s["minute"])
                     for s in BotBrain.SCHEDULE["article_slots"]}
        scheduled |= {(s["hour"], s["minute"])
                      for s in BotBrain.SCHEDULE["evergreen_slots"]}
        self.assertEqual(set(S.SLOT_PRIORITY), scheduled)

    def test_an_out_of_favour_slot_is_skipped_while_warming_up(self):
        syn = self._syn(services=["facebook"], caps={"facebook": 8})
        syn.daily_cap = lambda platform: 4
        # 13:00 PKT is the lowest-ranked slot: outside the top four.
        thirteen = datetime(2026, 9, 1, 13, 2, tzinfo=timezone.utc)
        self.assertFalse(syn._slot_is_allowed("facebook", thirteen))
        # 18:00 PKT is the best one.
        self.assertTrue(syn._slot_is_allowed(
            "facebook", datetime(2026, 9, 1, 18, 1, tzinfo=timezone.utc)))

    def test_a_deferred_article_still_counts_as_its_own_slot(self):
        """A story held back 30 minutes for a picture must not lose its share
        just because it published at 18:30 instead of 18:00."""
        syn = self._syn(services=["facebook"])
        syn.daily_cap = lambda platform: 4
        self.assertTrue(syn._slot_is_allowed(
            "facebook", datetime(2026, 9, 1, 18, 34, tzinfo=timezone.utc)))

    def test_an_off_schedule_publish_is_allowed_on_the_counter_alone(self):
        """A manual run at 09:00 belongs to no slot; the daily cap is then the
        only thing standing between it and a post."""
        syn = self._syn(services=["facebook"])
        syn.daily_cap = lambda platform: 4
        self.assertTrue(syn._slot_is_allowed(
            "facebook", datetime(2026, 9, 1, 8, 45, tzinfo=timezone.utc)))

    def test_the_slot_gate_is_off_once_the_cap_covers_every_slot(self):
        syn = self._syn(services=["facebook"], caps={"facebook": 8})
        for hour in range(24):
            self.assertTrue(syn._slot_is_allowed(
                "facebook", datetime(2026, 9, 1, hour, 7, tzinfo=timezone.utc)))

    # ── isolation ────────────────────────────────────────────────

    def test_facebook_failing_does_not_stop_x(self):
        buf = MagicMock()
        buf.ensure_channels = AsyncMock(
            side_effect=lambda s: [{"id": s, "service": s}])
        buf.last_error = ""

        async def send(channel, text, image_url="", article_slug=""):
            if channel["service"] == "facebook":
                raise RuntimeError("facebook down")
            return True

        buf.send = AsyncMock(side_effect=send)
        syn = self._syn(buffer=buf)
        results = asyncio.run(syn.syndicate(self.ARTICLE))
        self.assertFalse(results["facebook"])
        self.assertTrue(results["twitter"])

    def test_posts_are_published_now_not_queued(self):
        """
        Two reasons, and the live account had already hit the second: a post
        must go out when its article does, and Buffer's free plan holds only
        TEN posts in a channel queue -- which eight articles a day fill in a
        day and a half. The account was stuck at "10 of 10 allowed" and had
        stopped accepting posts entirely.
        """
        from modules.buffer_broadcaster import BufferBroadcaster
        bb = BufferBroadcaster(access_token="tok")
        captured = {}

        async def fake_gql(query, variables=None, timeout=30):
            captured.update(variables or {})
            return {"createPost": {"__typename": "PostActionSuccess",
                                   "post": {"id": "p", "status": "sent"}}}

        bb._gql = fake_gql
        asyncio.run(bb.send({"id": "c", "service": "facebook"}, "hi"))
        self.assertEqual(captured["i"]["mode"], "shareNow")
        self.assertEqual(captured["i"]["schedulingType"], "automatic")

    def test_an_account_that_will_not_publish_now_falls_back_to_the_queue(self):
        """Queueing at the wrong time still beats losing the post."""
        from modules.buffer_broadcaster import BufferBroadcaster
        bb = BufferBroadcaster(access_token="tok")
        modes = []

        async def fake_gql(query, variables=None, timeout=30):
            modes.append(variables["i"]["mode"])
            if variables["i"]["mode"] == "shareNow":
                return {"createPost": {"__typename": "InvalidInputError",
                                       "message": "cannot publish immediately"}}
            return {"createPost": {"__typename": "PostActionSuccess",
                                   "post": {"id": "p", "status": "queued"}}}

        bb._gql = fake_gql
        self.assertTrue(asyncio.run(bb.send({"id": "c", "service": "facebook"}, "hi")))
        self.assertEqual(modes, ["shareNow", "addToQueue"])

    def test_the_fallback_is_tried_once_not_in_a_loop(self):
        from modules.buffer_broadcaster import BufferBroadcaster
        bb = BufferBroadcaster(access_token="tok")
        calls = []

        async def fake_gql(query, variables=None, timeout=30):
            calls.append(variables["i"]["mode"])
            return {"createPost": {"__typename": "LimitReachedError",
                                   "message": "Scheduled posts limit reached."}}

        bb._gql = fake_gql
        self.assertFalse(asyncio.run(bb.send({"id": "c", "service": "facebook"}, "hi")))
        self.assertEqual(len(calls), 2)
        self.assertIn("limit reached", bb.last_error)

    def test_a_channel_connected_after_start_up_is_picked_up(self):
        """
        The channel list is read once at boot. The user is connecting a brand
        new Facebook page and X account right now, and without a recheck they
        would stay invisible to the running bot until the next deploy, with
        nothing in the log to explain the silence.
        """
        from modules.buffer_broadcaster import BufferBroadcaster
        buf = BufferBroadcaster(access_token="tok", organization_id="org1",
                                enabled_services=["facebook"])
        buf._loaded_at = -10000          # older than REFRESH_AFTER_SECONDS

        async def fake_connect():
            buf.channels = [{"id": "c1", "service": "facebook", "name": "New Page",
                             "isDisconnected": False}]
            return True

        buf.connect = fake_connect
        self.assertEqual(buf.channels_for("facebook"), [])
        found = asyncio.run(buf.ensure_channels("facebook"))
        self.assertEqual([c["id"] for c in found], ["c1"])

    def test_a_permanently_absent_channel_is_not_rechecked_every_article(self):
        from modules.buffer_broadcaster import BufferBroadcaster
        buf = BufferBroadcaster(access_token="tok", organization_id="org1")
        buf._loaded_at = -10000
        calls = []

        async def fake_connect():
            calls.append(1)
            return True

        buf.connect = fake_connect
        asyncio.run(buf.ensure_channels("twitter"))
        asyncio.run(buf.ensure_channels("twitter"))
        asyncio.run(buf.ensure_channels("twitter"))
        self.assertEqual(len(calls), 1, "one recheck, then back off")

    def test_a_missing_channel_is_not_an_error(self):
        buf = MagicMock()
        buf.ensure_channels = AsyncMock(return_value=[])
        buf.send = AsyncMock(return_value=True)
        buf.last_error = ""
        syn = self._syn(buffer=buf)
        self.assertEqual(asyncio.run(syn.syndicate(self.ARTICLE)),
                         {"facebook": False, "twitter": False})
        buf.send.assert_not_awaited()

    def test_the_article_picture_is_passed_to_buffer(self):
        syn = self._syn(services=["facebook"])
        asyncio.run(syn.syndicate(self.ARTICLE))
        self.assertEqual(syn._buffer_mock.send.await_args.kwargs["image_url"],
                         "https://cdn.example/hero.jpg")
        self.assertEqual(syn._buffer_mock.send.await_args.kwargs["article_slug"],
                         "bitcoin-halving-explained")


class TestSocialSwitches(unittest.TestCase):
    """
    Three switches, not one. They fail for different reasons -- an account
    gets restricted, a page is being rebuilt -- and taking one platform off
    must not stop the other, nor stop the website publishing.
    """

    ARTICLE = TestSocialSyndicator.ARTICLE

    def _syn(self, brain, **kw):
        from modules.social_syndicator import SocialSyndicator
        buf = MagicMock()
        buf.ensure_channels = AsyncMock(
            side_effect=lambda s: [{"id": s, "service": s}])
        buf.send = AsyncMock(return_value=True)
        buf.last_error = ""
        syn = SocialSyndicator(buffer=buf, brain=brain,
                               site_url="https://pressvane.com",
                               services=["facebook", "twitter"], **kw)
        syn._slot_is_allowed = lambda *a: True
        syn._buffer_mock = buf
        return syn

    def test_social_starts_switched_off(self):
        self.assertFalse(make_brain().social_module_active)

    def test_the_master_switch_stops_both(self):
        brain = make_brain()          # social_module_active is False
        syn = self._syn(brain)
        self.assertEqual(asyncio.run(syn.syndicate(self.ARTICLE)), {})
        syn._buffer_mock.send.assert_not_awaited()

    def test_one_platform_can_be_switched_off_alone(self):
        brain = make_brain()
        brain.social_module_active = True
        brain.facebook_active = False
        syn = self._syn(brain)
        results = asyncio.run(syn.syndicate(self.ARTICLE))
        self.assertFalse(results["facebook"])
        self.assertTrue(results["twitter"])

    def test_x_can_be_switched_off_alone(self):
        brain = make_brain()
        brain.social_module_active = True
        brain.twitter_active = False
        syn = self._syn(brain)
        results = asyncio.run(syn.syndicate(self.ARTICLE))
        self.assertTrue(results["facebook"])
        self.assertFalse(results["twitter"])

    def test_x_answers_to_both_of_its_names(self):
        brain = make_brain()
        brain.social_module_active = True
        brain.twitter_active = False
        self.assertFalse(brain.social_enabled("x"))
        self.assertFalse(brain.social_enabled("twitter"))

    def test_the_kill_switch_beats_every_other_switch(self):
        brain = make_brain()
        brain.social_module_active = True
        brain.master_kill = True
        self.assertFalse(brain.social_enabled("facebook"))
        self.assertEqual(asyncio.run(self._syn(brain).syndicate(self.ARTICLE)), {})

    def test_social_being_off_does_not_stop_the_website(self):
        """The article still publishes; only the announcement is withheld."""
        brain = make_brain()
        brain.website_module_active = True
        brain.social_module_active = False
        agent = MagicMock()
        agent.generate_and_publish_article = AsyncMock(return_value={"slug": "s"})
        scraper = MagicMock()
        scraper.fetch_latest_news = AsyncMock(
            return_value=[{"title": "T", "link": "https://src/1"}])
        from modules.fanout import Fanout
        fo = Fanout(brain=brain, article_agent=agent, scraper=scraper,
                    syndicator=self._syn(brain), pick_category=lambda: "crypto")
        self.assertEqual(asyncio.run(fo.publish_scheduled_article())["slug"], "s")

    def test_the_switches_are_persisted_and_restored(self):
        brain = make_brain()
        brain.social_module_active = True
        brain.twitter_active = False
        snap = brain.snapshot()
        self.assertTrue(snap["social_module_active"])
        self.assertFalse(snap["twitter_active"])

        restored = make_brain()
        restored.db = MagicMock()
        restored.db.load_state = AsyncMock(return_value=snap)
        asyncio.run(restored.restore_state())
        self.assertTrue(restored.social_module_active)
        self.assertFalse(restored.twitter_active)
        self.assertTrue(restored.facebook_active)


class TestSocialPostsCarryTheStory(unittest.TestCase):
    """
    A post that is only a headline and a link asks for a click and gives
    nothing back. The Facebook post carries the opening of the article, so a
    reader who never clicks still learns what happened.
    """

    BODY = ("<h2>What changed</h2>"
            "<p>The Federal Reserve raised its benchmark rate by a quarter "
            "point on Wednesday, the third increase this year, and signalled "
            "that another may follow before December.</p>"
            "<p>Chair Kevin Warsh said inflation had proved more stubborn "
            "than the committee expected, particularly in services, where "
            "prices are still rising at an annual pace above four per cent.</p>"
            "<h2>What it means</h2>"
            "<p>Mortgage rates track the benchmark closely, so households "
            "renewing a fixed deal in the next year will feel this first.</p>"
            "<p class='photo-credit'><small>Photo: Reuters</small></p>")

    ARTICLE = {"slug": "fed-raises-rates-again", "title": "Fed raises rates again",
               "content": BODY, "summary": "The Fed raised rates a quarter point.",
               "seo_keywords": ["federal reserve", "interest rates"],
               "main_image_url": "https://cdn.example/hero.jpg"}

    def _syn(self):
        from modules.social_syndicator import SocialSyndicator
        return SocialSyndicator(buffer=MagicMock(), site_url="https://pressvane.com")

    def test_the_post_carries_whole_paragraphs_of_the_article(self):
        text = self._syn().facebook_caption(self.ARTICLE, "https://pressvane.com/s")
        self.assertIn("third increase this year", text)
        self.assertIn("more stubborn", text)
        self.assertIn("https://pressvane.com/s", text)

    def test_headings_and_photo_credits_are_left_out(self):
        text = self._syn().facebook_caption(self.ARTICLE, "https://pressvane.com/s")
        self.assertNotIn("What changed", text)
        self.assertNotIn("Photo: Reuters", text)

    def test_a_paragraph_is_never_cut_in_half(self):
        excerpt = self._syn().article_excerpt(self.ARTICLE, 260)
        self.assertTrue(excerpt)
        for para in excerpt.split("\n\n"):
            self.assertTrue(para.rstrip().endswith((".", "!", "?", '"')),
                            f"paragraph ends mid-sentence: {para[-40:]!r}")

    def test_an_article_with_no_body_still_posts(self):
        """Older records have no content column loaded; the summary stands in."""
        syn = self._syn()
        bare = {"slug": "s", "title": "A headline long enough to pass",
                "summary": "The Fed raised rates a quarter point on Wednesday."}
        text = syn.facebook_caption(bare, "https://pressvane.com/s")
        self.assertIn("quarter point", text)
        self.assertIn("https://pressvane.com/s", text)

    def test_the_excerpt_stays_within_facebooks_limit(self):
        syn = self._syn()
        huge = dict(self.ARTICLE,
                    content="<p>" + ("A long sentence that keeps going. " * 400) + "</p>")
        text = syn.facebook_caption(huge, "https://pressvane.com/s")
        self.assertLessEqual(len(text), syn.FACEBOOK_MAX_CHARS)
        self.assertIn("https://pressvane.com/s", text)


class TestSocialIsDrivenByTheArticle(unittest.TestCase):
    """
    The trigger is the article publishing, not the Telegram slot.

    Facebook used to hang off the Telegram fan-out. Once the website got its
    own schedule the two carried different stories, so those posts had no
    article to link to -- and when the schedules did collide, the same story
    went to Facebook twice.
    """

    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.main = open(os.path.join(root, "main.py"), encoding="utf-8").read()
        self.fanout = open(os.path.join(root, "modules", "fanout.py"),
                           encoding="utf-8").read()

    def test_the_telegram_fanout_no_longer_posts_to_facebook(self):
        self.assertNotIn("_to_facebook", self.fanout)
        self.assertNotIn('"facebook": self.', self.fanout)

    def test_the_article_run_hands_off_to_the_syndicator(self):
        i = self.fanout.index("Scheduled article published")
        self.assertIn("await self.syndicate(article)", self.fanout[i:i + 400])

    def test_a_deferred_article_is_announced_too(self):
        i = self.fanout.index("Deferred article published")
        self.assertIn("await self.syndicate(article)", self.fanout[i:i + 400])

    def test_evergreen_explainers_are_announced_too(self):
        i = self.main.index("Evergreen live:")
        self.assertIn("fanout.syndicate(piece)", self.main[i:i + 400])

    def test_the_syndicator_is_wired_into_main(self):
        self.assertIn("SocialSyndicator(", self.main)
        self.assertIn("syndicator=syndicator", self.main)

    def test_telegram_broadcasting_is_untouched_by_social(self):
        # The Telegram broadcaster must not learn about Buffer, Facebook or X.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tg = open(os.path.join(root, "modules", "telegram_broadcaster.py"),
                  encoding="utf-8").read().lower()
        for word in ("buffer", "facebook", "syndicat"):
            self.assertNotIn(word, tg)


# ═══════════════════════════════════════════════════════════════
#  META DESCRIPTIONS MUST END A SENTENCE
# ═══════════════════════════════════════════════════════════════
class TestMetaDescriptionEndsProperly(unittest.TestCase):
    """
    56 of the 63 articles on the live site had a meta description that
    stopped mid-clause: "...to reveal transaction volume, active addresses,
    and token age-spent, helping". That is the text Google prints under the
    headline, so it was the first thing a searcher saw.

    The cause is the prompt. It asks for 22-28 words, and the model obeys the
    count rather than the sentence -- it stops on whatever word it reached.
    Nothing caught it, because the old trimmer only ever fixed descriptions
    that were too LONG, and every one of these was under the limit.
    """

    ENDINGS = (".", "!", "?", "\u201d", "\u2019", '"')

    def setUp(self):
        from modules.article_engine import ArticleAgent
        self.A = ArticleAgent

    def fix(self, text, fallback=""):
        return self.A._finish_sentence(text, 160, fallback=fallback)

    def ends_ok(self, text):
        return bool(text) and text[-1] in self.ENDINGS

    def _record(self, meta_description):
        return {
            "title": "A headline that is definitely long enough",
            "content": "<p>" + ("Real sentence here. " * 60) + "</p>",
            "word_count": 400, "slug": "a-slug", "summary": "A summary.",
            "meta_title": "A meta title long enough to pass the checks",
            "seo_keywords": ["one thing", "two thing", "three thing"],
            "meta_description": meta_description,
        }

    def test_a_dangling_participle_is_cut_back_to_the_clause(self):
        out = self.fix("This article explains on-chain analysis, detailing how "
                       "blockchain data is examined to reveal transaction volume, "
                       "active addresses, and token age-spent, helping")
        self.assertTrue(self.ends_ok(out))
        self.assertNotIn("helping", out)
        self.assertIn("token age-spent", out)

    def test_a_trailing_clause_is_dropped_not_terminated(self):
        """
        "...and understand how these metrics" reads as unfinished, so adding
        a full stop there would publish broken English. Cutting back to the
        comma gives a sentence that is actually true.
        """
        out = self.fix("Learn how to decode a company earnings report by focusing "
                       "on revenue, earnings per share, operating margin, and free "
                       "cash flow, and understand how these metrics")
        self.assertTrue(self.ends_ok(out))
        self.assertNotIn("these metrics", out)
        self.assertTrue(out.endswith("free cash flow."))

    def test_a_finished_sentence_only_gains_its_full_stop(self):
        out = self.fix("Crypto companies push AI firms to give Bitcoin developers "
                       "early access to enhance security and integrity")
        self.assertEqual(out[-1], ".")
        self.assertIn("security and integrity", out)

    def test_one_sitting_exactly_on_the_limit_gives_up_a_word(self):
        """The full stop has to fit inside 160 too."""
        text = ("A fire in a public hospital's neonatal unit in Pakistan claimed "
                "14 newborn lives, sparking a national investigation and exposing "
                "critical failures in healthcare")
        self.assertEqual(len(text), 160)
        out = self.fix(text)
        self.assertLessEqual(len(out), 160)
        self.assertTrue(self.ends_ok(out))

    def test_a_run_of_dangling_words_unwinds_to_a_real_ending(self):
        """"...and potentially limiting in" is three unfinished words deep."""
        out = self.fix(
            "The Supreme Court 6-3 ruling supports a challenge to federal mail-in "
            "voting rules by tightening verification and potentially limiting in")
        self.assertTrue(self.ends_ok(out))
        self.assertTrue(out.endswith("tightening verification."), out)

    def test_the_summary_rescues_one_with_nothing_to_cut_back_to(self):
        """Too short to keep, nothing to cut to: the written summary stands in."""
        out = self.fix(
            "Supreme Court backs Trump on mail-in and",
            fallback="The Supreme Court upheld a rule tightening mail-in ballot "
                     "verification, allowing states more power to reject "
                     "improperly completed absentee votes.")
        self.assertTrue(self.ends_ok(out))
        self.assertIn("absentee votes", out)

    def test_a_deliberate_ellipsis_is_left_alone(self):
        text = ("ZEC traded above its January 2018 peak as futures volume hit "
                "billions of dollars and a Grayscale filing showed progress\u2026")
        self.assertEqual(self.fix(text), text)

    def test_html_entities_never_reach_a_search_result(self):
        out = self.fix("A view of the Azad Jammu and Kashmir Legislative Assembly "
                       "&mdash; the vote continues from 10am to 2pm at the assembly "
                       "building in Muzaffarabad")
        self.assertNotIn("&mdash;", out)
        self.assertIn("\u2014", out)

    def test_a_good_description_is_returned_untouched(self):
        good = ("Bitcoin fell to $78,630 before rebounding above $79,000 after "
                "Fed Chair Kevin Warsh's inflation speech.")
        self.assertEqual(self.fix(good), good)

    def test_the_result_is_always_within_googles_budget(self):
        for text in ("word " * 200, "A. " * 90, "no punctuation here at all " * 9):
            out = self.fix(text)
            self.assertLessEqual(len(out), 160, repr(text[:30]))

    def test_the_gate_now_catches_it(self):
        """
        The old check listed a handful of trailing words, so it missed
        "helping" entirely.
        """
        from modules.article_engine import ArticleAgent
        agent = ArticleAgent(ai_engine=MagicMock(), db=None, site_name="X")
        _, fixable = agent._quality_issues(
            self._record("x" * 40 + " and something that stops helping"))
        self.assertTrue(any("end a sentence" in f for f in fixable), fixable)

    def test_a_word_ending_in_and_is_not_a_dangling_and(self):
        """The old check matched "thousand" by substring."""
        from modules.article_engine import ArticleAgent
        agent = ArticleAgent(ai_engine=MagicMock(), db=None, site_name="X")
        _, fixable = agent._quality_issues(self._record(
            "Pakistan exported goods worth several billion dollars last year, a "
            "figure the ministry called a record for the decade and a thousand."))
        self.assertFalse(any("end a sentence" in f for f in fixable), fixable)

    def test_the_prompt_asks_for_complete_sentences(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "modules", "article_engine.py"),
                   encoding="utf-8").read()
        self.assertIn("COMPLETE sentences", src)
        self.assertIn("Never stop mid-sentence to hit the word", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
