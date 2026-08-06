"""
The Brain Module.
Central nervous system that controls scheduling, goal tracking,
dynamic throttling, sleep cycles, self-healing, and dashboard alerts.
"""
import logging
import asyncio
import random
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict
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
        "post_slots": [
            {"hour": 0, "minute": 40},   # 12:40 AM PKT (Test Post)
            {"hour": 10, "minute": 30},  # 10:30 AM
            {"hour": 13, "minute": 0},   # 1:00 PM
            {"hour": 16, "minute": 0},   # 4:00 PM
            {"hour": 19, "minute": 30},  # 7:30 PM
        ],
        "evening_wrap": {"hour": 21, "minute": 0},    # 9:00 PM PKT
        "sleep_start": 1,   # 1 AM PKT (Delayed to allow the test post)
        "sleep_end": 7,     # 7 AM PKT
    }
    
    def __init__(self, db: SupabaseDB, weekly_goal: int = 100, notification_manager=None):
        self.db = db
        self.weekly_goal = weekly_goal
        self.notification_manager = notification_manager
        self.current_subscribers = 0
        self.week_start_subscribers = 0
        
        # Daily counters (reset every midnight PKT)
        self.posts_today = 0
        self.replies_today = 0
        self.errors_today = 0
        self.auto_fixes_today = 0
        
        # Memory Tracking
        self.posted_categories = []
        self.posted_topics = []
        
        # Dynamic throttle limits
        self.max_posts_today = 6
        self.max_replies_today = 8
        
        # State
        self.is_sleeping = False
        self.is_paused = False
        self.last_post_time = None
        self.last_metric_check = None
        
        logger.info(f"Brain initialized. Weekly goal: {weekly_goal} subscribers.")
    
    def _get_pkt_now(self) -> datetime:
        """Returns current time in Pakistan Standard Time."""
        return datetime.now(timezone.utc) + PKT_OFFSET
    
    def is_sleep_time(self) -> bool:
        """
        Checks if the bot should be sleeping.
        Sleep hours: 11 PM to 7 AM PKT.
        """
        pkt_now = self._get_pkt_now()
        hour = pkt_now.hour
        
        if hour >= self.SCHEDULE["sleep_start"] or hour < self.SCHEDULE["sleep_end"]:
            if not self.is_sleeping:
                logger.info(f"Entering sleep mode. Current PKT time: {pkt_now.strftime('%I:%M %p')}")
                self.is_sleeping = True
            return True
        
        if self.is_sleeping:
            logger.info(f"Waking up! Current PKT time: {pkt_now.strftime('%I:%M %p')}")
            self.is_sleeping = False
        
        return False
    
    def get_next_post_slot(self) -> Optional[Dict]:
        """
        Determines the next scheduled post slot.
        Returns the slot info if it's time to post, None otherwise.
        """
        pkt_now = self._get_pkt_now()
        current_hour = pkt_now.hour
        current_minute = pkt_now.minute
        
        # Check morning brief
        mb = self.SCHEDULE["morning_brief"]
        if current_hour == mb["hour"] and current_minute >= mb["minute"] and current_minute < mb["minute"] + 15:
            return {"type": "morning_brief", "hour": mb["hour"]}
        
        # Check regular post slots
        for slot in self.SCHEDULE["post_slots"]:
            if current_hour == slot["hour"] and current_minute >= slot["minute"] and current_minute < slot["minute"] + 15:
                return {"type": "regular_post", "hour": slot["hour"]}
        
        # Check evening wrap
        ew = self.SCHEDULE["evening_wrap"]
        if current_hour == ew["hour"] and current_minute >= ew["minute"] and current_minute < ew["minute"] + 15:
            return {"type": "evening_wrap", "hour": ew["hour"]}
        
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
        self.errors_today = 0
        self.auto_fixes_today = 0
        self.posted_categories.clear()
        self.posted_topics.clear()
        self.max_replies_today = 8  # Reset to default
    
    def get_status_report(self) -> str:
        """Generates a human-readable status report."""
        pkt_now = self._get_pkt_now()
        gained = self.current_subscribers - self.week_start_subscribers
        
        report = (
            f"--- Daily Pulse PK Status ---\n"
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
