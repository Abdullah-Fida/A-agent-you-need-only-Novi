"""
Growth Engine — subscriber acquisition strategy and measurement.

This module answers the question the rest of the system never could:
"is what we are doing actually gaining subscribers, and which channel is
working?"

It does three things:

1. TRACKS   — records subscriber counts over time so growth is measurable
              instead of guessed.
2. ATTRIBUTES — records every growth ACTION (post, reply, invite) so we can
              tell which activity preceded growth.
3. ADVISES  — turns that history into a concrete recommendation, and can
              apply safe throttle adjustments automatically.

Design rule: this module never sends anything to Telegram itself. It only
measures and recommends. The modules that take actions stay in charge of
their own safety limits.
"""
import asyncio
import logging
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger("OmniBot.Growth")

PKT_OFFSET = timedelta(hours=5)


class GrowthEngine:
    """Measures subscriber growth and recommends what to do next."""

    # Actions we attribute growth to
    ACTION_POST = "post"
    ACTION_REPLY = "stealth_reply"
    ACTION_INVITE = "invite"
    ACTION_SIGNAL = "signal"

    def __init__(self, db=None, weekly_goal: int = 100, notification_manager=None):
        self.db = db
        self.weekly_goal = max(1, weekly_goal)
        self.nm = notification_manager

        # Rolling in-memory history (survives without a database)
        self._samples: deque = deque(maxlen=500)      # (datetime, subscriber_count)
        self._actions: deque = deque(maxlen=1000)     # (datetime, action, meta)

        self.week_start_count: Optional[int] = None
        self.week_start_at: Optional[datetime] = None
        self.last_count: Optional[int] = None

    # ── time helpers ──────────────────────────────────────────────
    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc) + PKT_OFFSET

    # ── recording ─────────────────────────────────────────────────
    async def record_subscribers(self, count: int):
        """Records a subscriber-count sample and persists it."""
        if count is None or count < 0:
            return

        now = self._now()
        previous = self.last_count
        self._samples.append((now, count))
        self.last_count = count

        if self.week_start_count is None:
            self.week_start_count = count
            self.week_start_at = now

        # Roll the weekly baseline every 7 days
        elif self.week_start_at and (now - self.week_start_at).days >= 7:
            gained = count - self.week_start_count
            logger.info(f"Week complete. Net growth: {gained:+d} subscribers.")
            if self.nm:
                await self.nm.send_notification(
                    subject=f"Weekly Growth Report: {gained:+d} subscribers",
                    message=(
                        f"Week ending {now.strftime('%b %d')}\n\n"
                        f"Started at: {self.week_start_count}\n"
                        f"Now: {count}\n"
                        f"Net change: {gained:+d}\n"
                        f"Goal was: {self.weekly_goal}\n"
                        f"Achieved: {(gained / self.weekly_goal * 100):.0f}% of goal"
                    ),
                    is_critical=False
                )
            self.week_start_count = count
            self.week_start_at = now

        if previous is not None and count != previous:
            logger.info(f"Subscriber change: {previous} -> {count} ({count - previous:+d})")

        await self._persist("subscriber_count", count)

    def record_action(self, action: str, meta: Dict = None):
        """Records that a growth action was taken (used for attribution)."""
        self._actions.append((self._now(), action, meta or {}))

    async def _persist(self, metric: str, value: float, meta: Dict = None):
        if not self.db or not getattr(self.db, "_initialized", False):
            return
        try:
            await self.db.log_metric(metric, value, meta or {})
        except Exception as e:
            logger.warning(f"Could not persist {metric}: {type(e).__name__}")

    # ── analysis ──────────────────────────────────────────────────
    def growth_since(self, hours: int) -> Optional[int]:
        """Net subscriber change over the last N hours, or None if unknown."""
        if self.last_count is None:
            return None
        cutoff = self._now() - timedelta(hours=hours)
        older = [c for (t, c) in self._samples if t <= cutoff]
        if not older:
            return None
        return self.last_count - older[-1]

    def actions_since(self, hours: int) -> Dict[str, int]:
        """Counts each action type in the last N hours."""
        cutoff = self._now() - timedelta(hours=hours)
        out: Dict[str, int] = {}
        for (t, action, _) in self._actions:
            if t >= cutoff:
                out[action] = out.get(action, 0) + 1
        return out

    def weekly_progress(self) -> Dict[str, Any]:
        """Progress toward the weekly subscriber goal."""
        if self.last_count is None or self.week_start_count is None:
            return {"known": False, "gained": 0, "goal": self.weekly_goal, "percent": 0.0}

        gained = self.last_count - self.week_start_count
        days_in = max(1, (self._now() - self.week_start_at).days + 1) if self.week_start_at else 1
        expected = self.weekly_goal * (days_in / 7)

        return {
            "known": True,
            "gained": gained,
            "goal": self.weekly_goal,
            "percent": round(gained / self.weekly_goal * 100, 1),
            "days_elapsed": days_in,
            "expected_by_now": round(expected, 1),
            "on_track": gained >= expected,
        }

    def recommendation(self) -> Dict[str, Any]:
        """
        Produces a concrete, human-readable recommendation about what to
        change, based on measured growth rather than guesswork.
        """
        prog = self.weekly_progress()
        day_growth = self.growth_since(24)
        acts = self.actions_since(24)

        if not prog["known"] or day_growth is None:
            return {
                "status": "measuring",
                "headline": "Still gathering data.",
                "detail": "Need at least 24 hours of subscriber samples before advising.",
                "suggested_changes": [],
            }

        changes: List[Dict[str, Any]] = []

        if day_growth <= 0:
            status = "stalled"
            headline = f"No growth in the last 24h ({day_growth:+d})."
            if acts.get(self.ACTION_POST, 0) < 3:
                changes.append({"what": "posts", "direction": "up",
                                "why": "Fewer than 3 posts went out in 24h — reach is too low."})
            if acts.get(self.ACTION_INVITE, 0) == 0:
                changes.append({"what": "invites", "direction": "up",
                                "why": "No invitations were sent. This is the most direct growth lever."})
            if not changes:
                changes.append({"what": "content", "direction": "review",
                                "why": "Activity levels are fine but nothing converted — the content angle likely needs work."})

        elif not prog["on_track"]:
            status = "behind"
            headline = (f"Growing ({day_growth:+d}/day) but behind the weekly goal "
                        f"({prog['gained']}/{prog['goal']}).")
            changes.append({"what": "invites", "direction": "up",
                            "why": f"Behind pace: expected ~{prog['expected_by_now']} by now."})
        else:
            status = "on_track"
            headline = (f"On track: {prog['gained']}/{prog['goal']} this week "
                        f"({day_growth:+d} in the last 24h).")
            changes.append({"what": "nothing", "direction": "hold",
                            "why": "Current strategy is working. Avoid changing limits while it is."})

        return {
            "status": status,
            "headline": headline,
            "detail": (f"24h growth: {day_growth:+d} | "
                       f"actions: {acts or 'none'} | "
                       f"week: {prog['gained']}/{prog['goal']} ({prog['percent']}%)"),
            "suggested_changes": changes,
            "weekly": prog,
            "actions_24h": acts,
        }

    def summary(self) -> Dict[str, Any]:
        """Everything the dashboard needs about growth, in one object."""
        return {
            "subscribers": self.last_count,
            "growth_24h": self.growth_since(24),
            "growth_7d": self.growth_since(24 * 7),
            "weekly": self.weekly_progress(),
            "actions_24h": self.actions_since(24),
            "recommendation": self.recommendation(),
            "samples_collected": len(self._samples),
        }
