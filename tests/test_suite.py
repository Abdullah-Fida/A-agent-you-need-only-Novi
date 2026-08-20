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
        b = BufferBroadcaster(access_token="t", db=MagicMock(),
                              site_url="https://example.com")
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
        asyncio.run(broadcaster._post_to_channel(
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

    def test_hosted_image_is_preferred_over_the_outlets_photo(self):
        from modules.buffer_broadcaster import BufferBroadcaster
        pick = BufferBroadcaster._pick_image
        self.assertEqual(
            pick({"image_url": "https://ours/a.jpg", "real_image_url": "https://theirs/b.jpg"}),
            "https://ours/a.jpg")
        self.assertEqual(pick({"real_image_url": "https://theirs/b.jpg"}),
                         "https://theirs/b.jpg")

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
        self.assertIn('category=package.get("category"', src)


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
                     "sm._reply_active", "sm._scraping_active"):
            self.assertNotIn(f"{flag} = not {flag}", src,
                             f"{flag} still blindly inverts")
        self.assertGreaterEqual(src.count("await _desired_state("), 5)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
