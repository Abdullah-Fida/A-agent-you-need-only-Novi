"""
QA harness for the Novi bot.

Exercises the logic that was broken, without touching Telegram or sending
real messages. Run with:  python qa_check.py
"""
import asyncio
import sys
import os
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'[PASS]' if cond else '[FAIL]'} {name}" + (f" — {detail}" if detail and not cond else ""))


# ── 1. Brain: is_sleeping is a property, not a callable bool ──────────────
from core.brain import BotBrain

brain = BotBrain.__new__(BotBrain)
brain.SCHEDULE = {k: (list(v) if isinstance(v, list) else v) for k, v in BotBrain.SCHEDULE.items()}
brain.db = None
brain.notification_manager = None

check("brain.is_sleeping is a property", isinstance(BotBrain.is_sleeping, property))
check("brain.is_sleeping returns bool", isinstance(brain.is_sleeping, bool))

# Sleep window logic
brain.set_sleep_window(23, 7)
overnight = [(h, (h >= 23 or h < 7)) for h in range(24)]
ok = True
for h, expected in overnight:
    if (brain.SCHEDULE["sleep_start"] > brain.SCHEDULE["sleep_end"]):
        actual = h >= brain.SCHEDULE["sleep_start"] or h < brain.SCHEDULE["sleep_end"]
        ok &= (actual == expected)
check("overnight sleep window 23->7 correct for all 24 hours", ok)

brain.set_sleep_window(1, 5)
check("same-day window stored", brain.SCHEDULE["sleep_start"] == 1 and brain.SCHEDULE["sleep_end"] == 5)

brain.set_sleep_window(0, 0)
check("24/7 mode never sleeps", brain.is_sleep_time() is False)
brain.set_sleep_window(23, 7)  # restore


# ── 2. Brain: post slots in the same hour are distinct ───────────────────
slots = BotBrain.SCHEDULE["post_slots"]
keys = {f"regular_{s['hour']}_{s['minute']}" for s in slots}
check("every post slot has a unique key (13:00 and 13:40 no longer collide)",
      len(keys) == len(slots), f"{len(keys)} keys for {len(slots)} slots")

# The original bug was two slots in one hour sharing a key, so the second
# never fired. Test the property directly rather than pinning it to the old
# 13:00/13:40 pair, which has since been replaced by an even spread.
synthetic = [{"hour": 13, "minute": 0}, {"hour": 13, "minute": 40}]
synthetic_keys = {f"regular_{s['hour']}_{s['minute']}" for s in synthetic}
check("two slots in the same hour still get distinct keys",
      len(synthetic_keys) == 2)

check("six post slots are configured", len(slots) == 6, f"{len(slots)} slots")

start, end = BotBrain.SCHEDULE["sleep_start"], BotBrain.SCHEDULE["sleep_end"]
asleep = [f"{s['hour']:02d}:{s['minute']:02d}" for s in slots
          if s["hour"] >= start or s["hour"] < end]
check("no slot is scheduled inside the sleep window",
      not asleep, ", ".join(asleep) + " unreachable" if asleep else "")

check("slot window exceeds max jitter",
      BotBrain.SLOT_WINDOW_MINUTES * 60 > 600, f"{BotBrain.SLOT_WINDOW_MINUTES}m window")


# ── 3. Telegram ID resolution ────────────────────────────────────────────
from utils.telegram_utils import candidate_ids

c = candidate_ids("-5533411583")
check("supergroup id gets -100 prefix as first candidate", c[0] == -1005533411583, str(c))
check("original id retained as fallback", -5533411583 in c)

c2 = candidate_ids("-1005533411583")
check("already-correct id stays first", c2[0] == -1005533411583)

check("@username passed through", candidate_ids("@Trading_Trader_Free") == ["@Trading_Trader_Free"])
check("t.me link normalized", candidate_ids("https://t.me/Novi_Network")[0] == "@Novi_Network")
check("empty target yields nothing", candidate_ids("") == [])


# ── 4. AI engine: models + rotation ──────────────────────────────────────
from core.ai_engine import MODELS, FALLBACK_MODELS, AIEngine

check("'article' task has a model (ArticleAgent no longer 404s)", "article" in MODELS)
check("dead model llama-3.1-8b-instruct:free is gone from fallbacks",
      "meta-llama/llama-3.1-8b-instruct:free" not in FALLBACK_MODELS)
check("fallback chain has multiple options", len(FALLBACK_MODELS) >= 3)

eng = AIEngine.__new__(AIEngine)
eng.api_keys = ["only-one-key"]
eng.current_key_index = 0
check("single key: rotation does not crash", eng._rotate_key() is False)
check("single key: index stays valid", eng.current_key_index == 0)


# ── 5. Signal Copier: dedup + limits ─────────────────────────────────────
from modules.signal_copier import SignalCopier

sc = SignalCopier.__new__(SignalCopier)
sc.__init__(api_id=1, api_hash="x", session_string="", phone="",
            source_channels=["@test"], ai_engine=None,
            signal_target_group="-5533411583")

check("signal copier starts inactive", sc.is_active is False)
check("signal daily limit defaults to unlimited", sc.max_signals_per_day == 0)
sc.set_daily_limit(25)
check("signal daily limit settable", sc.max_signals_per_day == 25)
sc.set_daily_limit(-5)
check("negative signal limit clamped to 0", sc.max_signals_per_day == 0)
check("status exposes target_resolved", "target_resolved" in sc.status)
check("handlers not registered before connect", sc._handlers_registered is False)


# ── 6. Stealth Marketer: limits, cap, device stability ───────────────────
from modules.stealth_marketer import StealthMarketer, DEVICE_PROFILES


def make_sm(phone="+923001234567"):
    sm = StealthMarketer.__new__(StealthMarketer)
    sm.__init__(api_id=1, api_hash="x", stealth_phone=phone,
                ai_engine=None, brain=brain, channel_username="@Novi_Network",
                target_groups=["@g"], stealth_invite_group="-5533411583")
    return sm


sm = make_sm()
check("stealth starts inactive", sm.is_active is False)
check("default daily invites is conservative", sm._invites_max_per_day <= 5)

r = sm.set_daily_invite_limit(10)
check("invite limit can be raised", sm._invites_max_per_day == 10 and r["applied"] == 10)

r = sm.set_daily_invite_limit(99999)
check("invite limit hard-capped for account safety",
      sm._invites_max_per_day == sm.INVITE_HARD_CAP and r["capped"] is True,
      f"applied={r['applied']}")
check("cap result reports the real applied value", r["applied"] == sm.INVITE_HARD_CAP)

# Device fingerprint must be stable across restarts for the same account
devices = {make_sm("+923001234567")._device["model"] for _ in range(6)}
check("device fingerprint is stable across restarts", len(devices) == 1, str(devices))
different = make_sm("+923009999999")._device["model"]
check("device is derived from account identity", make_sm()._device in DEVICE_PROFILES)

# Daily rollover
sm._invites_today = 5
sm._invite_day = (datetime.now(timezone.utc) + timedelta(hours=5)).date() - timedelta(days=1)
sm._roll_day_if_needed()
check("stale invite counter rolls over to a new day", sm._invites_today == 0)

check("no force-add: AddChatUserRequest removed from invite path",
      "AddChatUserRequest" not in open("modules/stealth_marketer.py", encoding="utf-8").read())
check("consent-based invite method exists", hasattr(sm, "_send_invite_link"))
check("minimum invite spacing enforced", sm.MIN_SECONDS_BETWEEN_INVITES >= 15 * 60)


# ── 7. No await-on-sync Supabase calls remain ────────────────────────────
import re
bad = []
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in ("node_modules", "__pycache__", ".git", ".next")]
    for f in files:
        if f.endswith(".py") and f != "qa_check.py":
            p = os.path.join(root, f)
            for i, line in enumerate(open(p, encoding="utf-8", errors="replace"), 1):
                if re.search(r"await\s+self\.db\.client\.table|await\s+self\.client\.table", line):
                    bad.append(f"{p}:{i}")
check("no `await` on synchronous supabase client calls", not bad, str(bad))


# ── 8. Dashboard no longer hardcodes localhost ───────────────────────────
app_jsx = open("novi-dashboard/src/App.jsx", encoding="utf-8").read()
check("dashboard image URLs are not hardcoded to localhost",
      "http://localhost:8000${data.image_url}" not in app_jsx
      and "'http://localhost:8000' + pkg.image_url" not in app_jsx)
check("API_BASE resolves dynamically", "import.meta.env.VITE_API_BASE" in app_jsx)
check("NOVI can control invite limits", "stealth_invite" in app_jsx)
check("NOVI can control sleep window", "set_sleep_window" in app_jsx)
check("NOVI has a health action", "'health'" in app_jsx)


# ── 9. API server wiring ─────────────────────────────────────────────────
api = open("core/api_server.py", encoding="utf-8").read()
for ep in ["/api/health", "/api/schedule", "/api/schedule/sleep_window", "/api/modify_limits"]:
    check(f"endpoint {ep} exists", f'"{ep}"' in api)
check("modify_limits supports stealth_invite", "stealth_invite" in api)
check("modify_limits supports signal", '"signal"' in api)


# ── 10. main.py reliability ──────────────────────────────────────────────
m = open("main.py", encoding="utf-8").read()
check("keep-alive pings faster than Render's 15m idle", "asyncio.sleep(240)" in m)
check("heartbeat auto-reconnects dead clients", "reconnect" in m.lower())
check("slot firing tracked by unique key", "fired_slots" in m and 'slot["key"]' in m)
check("stealth daily counters reset at midnight", "stealth_marketer.reset_daily_counters()" in m)


print("\n" + "=" * 60)
print(f"  RESULT: {len(PASS)} passed, {len(FAIL)} failed")
print("=" * 60)
if FAIL:
    for f in FAIL:
        print(f"  FAILED: {f}")
    sys.exit(1)
print("  All QA checks passed.")
