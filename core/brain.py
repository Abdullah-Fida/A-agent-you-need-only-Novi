"""
The Brain Module.
Central nervous system that controls scheduling, goal tracking,
dynamic throttling, sleep cycles, self-healing, and dashboard alerts.
"""
import logging
import asyncio
import random
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List
from database.supabase_db import SupabaseDB

logger = logging.getLogger("OmniBot.Brain")

# Pakistan Standard Time offset (UTC+5)
PKT_OFFSET = timedelta(hours=5)


class BotBrain:
    """
    The central controller that governs the entire bot's behavior.
    
    Responsibilities:
    - Enforces circadian sleep/wake cycles
    - Tracks weekly subscriber goals
    - Dynamically adjusts posting/reply throttles
    - Monitors for errors and triggers self-healing or alerts
    - Manages the daily posting schedule
    """
    
    # Default daily schedule (PKT times)
    SCHEDULE = {
        "morning_brief": {"hour": 8, "minute": 0},   # 8:00 AM PKT
        # Six slots, timed for the audience rather than for Pakistan.
        #
        # The website is the product and its readers are in the UK, Europe and
        # the United States, so the slots are chosen by what they read in UTC,
        # not by what hour it happens to be locally. The old spread put three
        # of six posts out at midnight, 02:30 and 05:00 New York time -- into
        # an empty room.
        #
        # Every slot must also sit outside the 23:00-07:00 PKT sleep window,
        # which is 14:00-22:00 in New York; that window is why nothing can be
        # published during the US evening without decoupling the article agent
        # from the Telegram schedule entirely.
        #
        #     PKT     UTC    London  New York
        "post_slots": [
            {"hour": 11, "minute": 30},  # 06:30   07:30   02:30  UK commute
            {"hour": 14, "minute": 0},   # 09:00   10:00   05:00  UK morning
            {"hour": 16, "minute": 0},   # 11:00   12:00   07:00  US wakes
            {"hour": 18, "minute": 0},   # 13:00   14:00   09:00  US PEAK
            {"hour": 20, "minute": 0},   # 15:00   16:00   11:00  US late morning
            {"hour": 22, "minute": 0},   # 17:00   18:00   13:00  US lunch
        ],
        "evening_wrap": {"hour": 21, "minute": 0},    # 9:00 PM PKT
        "sleep_start": 23,  # 11 PM PKT
        "sleep_end": 7,     # 7 AM PKT

        # The WEBSITE runs on its own clock.
        #
        # A Telegram channel has to behave like a person: it sleeps, and it
        # posts at hours that look plausible where it lives. A website has no
        # such constraint -- nobody sees when a page was uploaded, only when
        # they search for it -- so tying articles to the Telegram schedule
        # meant the entire US afternoon and evening, which is 14:00-22:00 in
        # New York and the sleep window here, could never carry an article.
        #
        # These six deliberately run through that window.
        #
        #     PKT     UTC    London  New York
        "article_slots": [
            {"hour": 11, "minute": 30},  # 06:30   07:30   02:30  UK commute
            {"hour": 15, "minute": 0},   # 10:00   11:00   06:00  US wakes
            {"hour": 18, "minute": 0},   # 13:00   14:00   09:00  US PEAK
            {"hour": 21, "minute": 0},   # 16:00   17:00   12:00  US lunch
            {"hour": 1,  "minute": 0},   # 20:00   21:00   16:00  US afternoon
            {"hour": 4,  "minute": 0},   # 23:00   00:00   19:00  US evening
        ],

        # Explainers. Two a day, off-peak from the news slots so the two
        # desks never compete for the same minute, and placed where the
        # audience is largest -- an explainer is found by search months
        # later, but the first day still helps.
        #
        #     PKT     UTC    London  New York
        "evergreen_slots": [
            {"hour": 13, "minute": 0},   # 08:00   09:00   04:00
            {"hour": 19, "minute": 30},  # 14:30   15:30   10:30  US morning
        ],
    }

    # How long a post slot stays "open" after its scheduled minute. This must
    # comfortably exceed the random jitter applied before posting, otherwise a
    # slot can expire while the bot is still waiting out its jitter.
    SLOT_WINDOW_MINUTES = 25
    
    def __init__(self, db: SupabaseDB, weekly_goal: int = 100, notification_manager=None,
                 config=None):
        self.db = db
        self.weekly_goal = weekly_goal
        self.notification_manager = notification_manager
        self.config = config
        self.current_subscribers = 0
        self.week_start_subscribers = 0

        # Daily counters (reset every midnight PKT)
        self.posts_today = 0
        self.pending_retries: List[Dict] = []
        self.replies_today = 0
        self.reddit_posts_today = 0
        self.x_posts_today = 0
        self.errors_today = 0
        self.auto_fixes_today = 0

        # Memory Tracking
        self.posted_categories = []
        self.posted_topics = []

        # Daily caps — sourced from config so the values in .env actually
        # take effect. Previously these were hardcoded and the configured
        # MAX_DAILY_* settings did nothing at all.
        self.max_posts_today = getattr(config, "max_daily_posts", 6)
        self.max_replies_today = getattr(config, "max_daily_telegram_replies", 8)
        self.max_reddit_posts_today = getattr(config, "max_daily_reddit_posts", 2)
        self.max_x_posts_today = getattr(config, "max_daily_x_posts", 5)
        # Remember the configured defaults so midnight resets restore them
        self._default_max_replies = self.max_replies_today

        if config is not None:
            self.SCHEDULE = dict(self.SCHEDULE)
            self.SCHEDULE["post_slots"] = list(self.SCHEDULE["post_slots"])
            self.SCHEDULE["sleep_start"] = getattr(config, "sleep_start_hour", 23)
            self.SCHEDULE["sleep_end"] = getattr(config, "sleep_end_hour", 7)
        
        # State
        self.is_paused = False
        self.last_post_time = None
        self.last_metric_check = None
        
        # Module Control Flags (toggled from Novi Dashboard)
        self.news_module_active = False     # News Agent starts OFF
        # Website / auto-blogging starts OFF. While off the Article Agent does
        # not run at all — no article is written and nothing is published to
        # the site, even when the News Agent is posting.
        self.website_module_active = False
        # AliExpress -> Pinterest agent. Off by default: it spends an
        # affiliate API quota and posts to a public account, so it only
        # runs when it has been switched on deliberately.
        self.pin_module_active = False

        # Facebook and X. Three switches rather than one, because they fail
        # for different reasons: an account gets restricted, or a page is
        # being rebuilt, and taking that platform off should not stop the
        # other one or stop the website publishing. All start OFF — they post
        # publicly under your name.
        self.social_module_active = False   # the master switch for all three
        self.facebook_active = True         # only meaningful while social is on
        self.twitter_active = True
        self.threads_active = True
        # The first day either of them posted, "YYYY-MM-DD". Recorded on the
        # first post and persisted, so the warm-up ramp starts itself and
        # survives a redeploy. Nobody has to remember to set a date.
        self.social_started_on = ""
        self.master_kill = False            # Master kill switch
        
        logger.info(f"Brain initialized. Weekly goal: {weekly_goal} subscribers.")
    
    def _get_pkt_now(self) -> datetime:
        """Returns current time in Pakistan Standard Time."""
        return datetime.now(timezone.utc) + PKT_OFFSET
    
    def is_sleep_time(self) -> bool:
        """
        Checks if the bot should be sleeping.

        Reads the sleep window from SCHEDULE so it can be changed at runtime
        (via the dashboard / NOVI) without restarting the bot. Handles both
        overnight windows (23 -> 7) and same-day windows (1 -> 5).

        If sleep_start == sleep_end the bot never sleeps (24/7 operation).
        """
        start = self.SCHEDULE["sleep_start"]
        end = self.SCHEDULE["sleep_end"]

        if start == end:
            return False  # 24/7 mode

        hour = self._get_pkt_now().hour
        if start > end:
            # Overnight window, e.g. 23:00 -> 07:00
            return hour >= start or hour < end
        # Same-day window, e.g. 01:00 -> 05:00
        return start <= hour < end

    @property
    def is_sleeping(self) -> bool:
        """
        Live sleep state. This is a property (not a stored flag) so the
        dashboard and the Stealth Marketer always see the real current
        state instead of a value that was never updated.
        """
        return self.is_sleep_time()

    def set_sleep_window(self, start_hour: int, end_hour: int):
        """
        Updates the sleep window at runtime. Pass equal values to disable
        sleeping entirely (24/7 operation).
        """
        start_hour = max(0, min(23, int(start_hour)))
        end_hour = max(0, min(23, int(end_hour)))
        self.SCHEDULE["sleep_start"] = start_hour
        self.SCHEDULE["sleep_end"] = end_hour
        logger.info(f"Sleep window updated: {start_hour}:00 -> {end_hour}:00 PKT "
                    f"({'24/7 mode — never sleeps' if start_hour == end_hour else 'active'})")
    
    # ── deferred posts ────────────────────────────────────────────────
    #
    # A slot used to be marked fired before the post was attempted, so any
    # failure — no image, AI unavailable, Telegram hiccup — silently cost a
    # post for the day. Failures are now re-queued instead.
    RETRY_DELAY_MINUTES = 30
    MAX_POST_ATTEMPTS = 3          # the original try plus two retries

    def queue_retry(self, key: str, post_type: str, attempts: int,
                    reason: str = "") -> bool:
        """
        Schedules another attempt at a post that could not be published.

        Returns False once the attempt budget is spent, which tells the caller
        to publish whatever it has rather than lose the slot entirely.
        """
        if attempts >= self.MAX_POST_ATTEMPTS:
            logger.warning(f"Slot {key} exhausted its {self.MAX_POST_ATTEMPTS} "
                           f"attempts; not retrying again.")
            return False

        due = self._get_pkt_now() + timedelta(minutes=self.RETRY_DELAY_MINUTES)
        self.pending_retries = [r for r in self.pending_retries if r["key"] != key]
        self.pending_retries.append({
            "key": key,
            "type": post_type,
            "attempts": attempts,
            "due_at": due.isoformat(),
            "reason": reason,
        })
        logger.info(f"Post for slot {key} deferred {self.RETRY_DELAY_MINUTES} min "
                    f"(attempt {attempts + 1}/{self.MAX_POST_ATTEMPTS}) — {reason}")
        return True

    def due_retry(self) -> Optional[Dict]:
        """The next deferred post whose delay has elapsed, if any."""
        now = self._get_pkt_now()
        for retry in list(self.pending_retries):
            try:
                due = datetime.fromisoformat(retry["due_at"])
            except (ValueError, KeyError):
                self.pending_retries.remove(retry)
                continue
            if now >= due:
                self.pending_retries.remove(retry)
                return retry
        return None

    def clear_retries(self):
        self.pending_retries = []

    def get_next_post_slot(self) -> Optional[Dict]:
        """
        Determines the next scheduled post slot.
        Returns the slot info if it's time to post, None otherwise.
        """
        pkt_now = self._get_pkt_now()
        current_hour = pkt_now.hour
        current_minute = pkt_now.minute

        # Each slot gets a unique "key" (not just the hour). Two slots in the
        # same hour — e.g. 13:00 and 13:40 — previously collapsed into one, so
        # the second one was silently skipped every single day.
        def in_window(slot) -> bool:
            return (current_hour == slot["hour"]
                    and slot["minute"] <= current_minute < slot["minute"] + self.SLOT_WINDOW_MINUTES)

        mb = self.SCHEDULE["morning_brief"]
        if in_window(mb):
            return {"type": "morning_brief", "hour": mb["hour"],
                    "key": f"morning_brief_{mb['hour']}_{mb['minute']}"}

        for slot in self.SCHEDULE["post_slots"]:
            if in_window(slot):
                return {"type": "regular_post", "hour": slot["hour"],
                        "key": f"regular_{slot['hour']}_{slot['minute']}"}

        ew = self.SCHEDULE["evening_wrap"]
        if in_window(ew):
            return {"type": "evening_wrap", "hour": ew["hour"],
                    "key": f"evening_wrap_{ew['hour']}_{ew['minute']}"}

        return None
    
    def get_due_article_slot(self) -> Optional[Dict]:
        """
        The website's own slot, if one is due.

        Deliberately ignores the sleep window and the Telegram post limit.
        Neither applies to a web page: the sleep window exists so the Telegram
        account looks human, and the post limit protects that account from
        looking like a bot. A published article is read whenever someone
        searches for it.
        """
        pkt_now = self._get_pkt_now()
        for slot in self.SCHEDULE.get("article_slots", []):
            if (pkt_now.hour == slot["hour"]
                    and slot["minute"] <= pkt_now.minute
                    < slot["minute"] + self.SLOT_WINDOW_MINUTES):
                return {"type": "article", "hour": slot["hour"],
                        "key": f"article_{slot['hour']}_{slot['minute']}"}
        return None

    def get_due_evergreen_slot(self) -> Optional[Dict]:
        """The explainer slot, if one is due. Same rules as the news slots."""
        pkt_now = self._get_pkt_now()
        for slot in self.SCHEDULE.get("evergreen_slots", []):
            if (pkt_now.hour == slot["hour"]
                    and slot["minute"] <= pkt_now.minute
                    < slot["minute"] + self.SLOT_WINDOW_MINUTES):
                return {"type": "evergreen", "hour": slot["hour"],
                        "key": f"evergreen_{slot['hour']}_{slot['minute']}"}
        return None

    def can_post(self) -> bool:
        """Checks if we're within daily post limits."""
        if self.posts_today >= self.max_posts_today:
            logger.info(f"Daily post limit reached ({self.posts_today}/{self.max_posts_today}).")
            return False
        return True
    
    def record_post(self, category: str = "general", topic: str = "general"):
        """Records that a post was made, tracking category and topic."""
        self.posts_today += 1
        self.last_post_time = self._get_pkt_now()
        self.posted_categories.append(category)
        self.posted_topics.append(topic)
        logger.info(f"Post recorded [{category}]. Today's total: {self.posts_today}/{self.max_posts_today}")
    
    def can_reply(self) -> bool:
        """Checks if we're within daily reply limits (for Stealth Marketer)."""
        return self.replies_today < self.max_replies_today

    def record_reply(self):
        """Records that a stealth reply was sent."""
        self.replies_today += 1

    def can_post_reddit(self) -> bool:
        """Enforces MAX_DAILY_REDDIT_POSTS (previously configured but ignored)."""
        if self.reddit_posts_today >= self.max_reddit_posts_today:
            logger.info(f"Reddit daily limit reached "
                        f"({self.reddit_posts_today}/{self.max_reddit_posts_today}).")
            return False
        return True

    def record_reddit_post(self):
        self.reddit_posts_today += 1

    def social_enabled(self, platform: str) -> bool:
        """Whether one social platform may post right now."""
        if self.master_kill or not self.social_module_active:
            return False
        if platform == "facebook":
            return self.facebook_active
        if platform in ("twitter", "x"):
            return self.twitter_active
        if platform == "threads":
            return self.threads_active
        return False

    def note_social_start(self) -> str:
        """
        Stamps today as day one, the first time anything is posted.

        The warm-up ramp needs to know how old the accounts are. Asking the
        operator to set a date is a step that gets forgotten, and forgetting
        it means eight posts a day out of a page with no history — the exact
        thing the ramp exists to prevent. So the first post records it.
        """
        if not self.social_started_on:
            self.social_started_on = self._get_pkt_now().date().isoformat()
            logger.info(f"First social post. Day one is "
                        f"{self.social_started_on}; the daily limit now ramps "
                        f"up on its own over the next fortnight.")
        return self.social_started_on

    def can_post_x(self) -> bool:
        """Enforces MAX_DAILY_X_POSTS (previously configured but ignored)."""
        if self.x_posts_today >= self.max_x_posts_today:
            logger.info(f"X/Twitter daily limit reached "
                        f"({self.x_posts_today}/{self.max_x_posts_today}).")
            return False
        return True

    def record_x_post(self):
        self.x_posts_today += 1
    
    async def ingest_metrics(self, subscriber_count: int):
        """
        Ingests current metrics and adjusts throttle limits accordingly.
        Called every few hours.
        """
        self.current_subscribers = subscriber_count
        
        # Calculate progress toward weekly goal
        gained_this_week = self.current_subscribers - self.week_start_subscribers
        progress_pct = (gained_this_week / max(self.weekly_goal, 1)) * 100
        
        logger.info(f"Metrics: {subscriber_count} subscribers | "
                     f"Weekly progress: {gained_this_week}/{self.weekly_goal} ({progress_pct:.0f}%)")
        
        # Dynamic throttle adjustment
        if progress_pct < 30:
            # Behind schedule — be slightly more active
            old_val = self.max_replies_today
            self.max_replies_today = min(12, self.max_replies_today + 2)
            msg = f"Behind schedule. Increased reply limit to {self.max_replies_today}."
            logger.info(msg)
            if self.notification_manager:
                await self.notification_manager.notify_strategy_change(
                    change_type="Stealth Reply Limit Increased",
                    old_value=str(old_val),
                    new_value=str(self.max_replies_today),
                    reason=f"Weekly progress at {progress_pct:.0f}% — behind schedule"
                )
        elif progress_pct > 80:
            # Ahead of schedule — relax and play it safe
            old_val = self.max_replies_today
            self.max_replies_today = max(4, self.max_replies_today - 2)
            msg = f"Ahead of schedule! Reduced reply limit to {self.max_replies_today}."
            logger.info(msg)
            if self.notification_manager:
                await self.notification_manager.notify_strategy_change(
                    change_type="Stealth Reply Limit Decreased",
                    old_value=str(old_val),
                    new_value=str(self.max_replies_today),
                    reason=f"Weekly progress at {progress_pct:.0f}% — ahead of schedule"
                )
        
        # Log to Supabase
        if self.db:
            await self.db.log_metric("subscriber_count", subscriber_count)
            await self.db.log_metric("weekly_progress_pct", progress_pct)
            await self.db.log_metric("posts_today", self.posts_today)
            await self.db.log_metric("replies_today", self.replies_today)
    
    async def handle_error(self, module: str, error: Exception, 
                           can_auto_fix: bool = False, fix_action: str = ""):
        """
        Central error handler for the entire system.
        Decides whether to auto-fix or escalate to the dashboard.
        """
        self.errors_today += 1
        error_msg = str(error)
        
        if can_auto_fix:
            self.auto_fixes_today += 1
            logger.warning(f"Self-healing: {module} error auto-fixed. Action: {fix_action}")
            # Send email for auto-fixed errors too
            if self.notification_manager:
                await self.notification_manager.notify_error(
                    module=module, error=error, auto_fixed=True, fix_action=fix_action
                )
            if self.db:
                await self.db.log_error(
                    module=module,
                    error_type=type(error).__name__,
                    error_message=f"{error_msg} | Auto-fix: {fix_action}",
                    auto_resolved=True
                )
        else:
            logger.error(f"UNRESOLVED ERROR in {module}: {error_msg}")
            # Send email for every error
            if self.notification_manager:
                await self.notification_manager.notify_error(
                    module=module, error=error, auto_fixed=False
                )
            if self.db:
                await self.db.log_error(
                    module=module,
                    error_type=type(error).__name__,
                    error_message=error_msg,
                    auto_resolved=False
                )
                # Push critical alert to dashboard
                await self.db.log_alert(
                    level="CRITICAL",
                    module=module,
                    message=f"Unresolved error requires attention: {error_msg[:300]}"
                )
    
    def reset_daily_counters(self):
        """Resets all daily counters. Called at midnight PKT."""
        logger.info(f"Midnight reset. Today's summary: "
                     f"Posts={self.posts_today}, Replies={self.replies_today}, "
                     f"Errors={self.errors_today}, AutoFixes={self.auto_fixes_today}")
        self.posts_today = 0
        self.replies_today = 0
        self.reddit_posts_today = 0
        self.x_posts_today = 0
        self.errors_today = 0
        self.auto_fixes_today = 0
        self.posted_categories.clear()
        self.posted_topics.clear()
        # Restore the CONFIGURED default, not a hardcoded 8
        self.max_replies_today = self._default_max_replies
    
    # ══════════════════════════════════════════════════════════
    #  STATE PERSISTENCE — survives restarts and redeploys
    # ══════════════════════════════════════════════════════════

    def snapshot(self) -> Dict:
        """The state worth restoring after a restart."""
        return {
            "news_module_active": self.news_module_active,
            "website_module_active": self.website_module_active,
            "pin_module_active": self.pin_module_active,
            "social_module_active": self.social_module_active,
            "facebook_active": self.facebook_active,
            "twitter_active": self.twitter_active,
            "threads_active": self.threads_active,
            "social_started_on": self.social_started_on,
            "master_kill": self.master_kill,
            "is_paused": self.is_paused,
            "max_posts_today": self.max_posts_today,
            "max_replies_today": self.max_replies_today,
            "max_reddit_posts_today": self.max_reddit_posts_today,
            "max_x_posts_today": self.max_x_posts_today,
            "sleep_start": self.SCHEDULE["sleep_start"],
            "sleep_end": self.SCHEDULE["sleep_end"],
        }

    async def save_state(self):
        """Persists the current configuration to Supabase."""
        if not self.db:
            return
        try:
            await self.db.save_state("brain", self.snapshot())
        except Exception as e:
            logger.warning(f"Could not persist brain state: {type(e).__name__}")

    async def restore_state(self):
        """
        Restores settings saved before the last restart.

        Without this, every redeploy silently reverted the news agent to OFF
        and every limit back to its default, undoing dashboard changes.
        """
        if not self.db:
            return
        try:
            saved = await self.db.load_state("brain")
        except Exception as e:
            logger.warning(f"Could not load brain state: {type(e).__name__}")
            return

        if not saved:
            logger.info("No saved brain state; using configured defaults.")
            return

        self.news_module_active = bool(saved.get("news_module_active", self.news_module_active))
        self.website_module_active = bool(saved.get("website_module_active", False))
        self.pin_module_active = bool(saved.get("pin_module_active", False))
        self.social_module_active = bool(saved.get("social_module_active", False))
        self.facebook_active = bool(saved.get("facebook_active", True))
        self.twitter_active = bool(saved.get("twitter_active", True))
        self.threads_active = bool(saved.get("threads_active", True))
        self.social_started_on = str(saved.get("social_started_on", "") or "")
        self.master_kill = bool(saved.get("master_kill", False))
        self.is_paused = bool(saved.get("is_paused", False))
        self.max_posts_today = int(saved.get("max_posts_today", self.max_posts_today))
        self.max_replies_today = int(saved.get("max_replies_today", self.max_replies_today))
        self.max_reddit_posts_today = int(saved.get("max_reddit_posts_today", self.max_reddit_posts_today))
        self.max_x_posts_today = int(saved.get("max_x_posts_today", self.max_x_posts_today))
        self.SCHEDULE["sleep_start"] = int(saved.get("sleep_start", self.SCHEDULE["sleep_start"]))
        self.SCHEDULE["sleep_end"] = int(saved.get("sleep_end", self.SCHEDULE["sleep_end"]))

        logger.info(
            f"Brain state restored — news={'ON' if self.news_module_active else 'OFF'}, "
            f"pins={'ON' if self.pin_module_active else 'OFF'}, "
            f"posts/day={self.max_posts_today}, sleep={self.SCHEDULE['sleep_start']}->"
            f"{self.SCHEDULE['sleep_end']}, master_kill={self.master_kill}"
        )

    def get_status_report(self) -> str:
        """Generates a human-readable status report."""
        pkt_now = self._get_pkt_now()
        gained = self.current_subscribers - self.week_start_subscribers
        
        report = (
            f"--- Novi News Status ---\n"
            f"Time: {pkt_now.strftime('%I:%M %p PKT, %b %d')}\n"
            f"Subscribers: {self.current_subscribers}\n"
            f"Weekly Goal: {gained}/{self.weekly_goal} "
            f"({(gained/max(self.weekly_goal,1)*100):.0f}%)\n"
            f"Posts Today: {self.posts_today}/{self.max_posts_today}\n"
            f"Replies Today: {self.replies_today}/{self.max_replies_today}\n"
            f"Errors: {self.errors_today} ({self.auto_fixes_today} auto-fixed)\n"
            f"Status: {'SLEEPING' if self.is_sleeping else 'ACTIVE'}\n"
        )
        return report
