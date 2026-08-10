"""
Real-Time Crypto Signal Copier.
Listens to designated competitor channels, instantly cleanses the signals
using AI (removing branding/links), and forwards them to the target group.
"""
import asyncio
import hashlib
import logging
import random
import os
import tempfile
from collections import deque
from typing import List, Dict, Any, Optional
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.errors import FloodWaitError

from utils.telegram_utils import resolve_chat

logger = logging.getLogger("OmniBot.SignalCopier")


class SignalCopier:
    """
    Real-Time Crypto Signal Copier.
    Listens to source channels and copies cleansed signals to a target group.
    Posts directly using its own Telethon client (not via TelegramBroadcaster).
    """

    def __init__(self, api_id: int, api_hash: str, session_string: str, phone: str,
                 source_channels: List[str], ai_engine: Any,
                 signal_target_group: str = "",
                 notification_manager: Any = None, db: Any = None,
                 channel_username: str = ""):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_string = session_string
        self.phone = phone
        self.source_channels = source_channels
        self.ai = ai_engine
        self.signal_target_group = signal_target_group
        self.nm = notification_manager
        self.db = db
        self.channel_username = channel_username

        self.client: Optional[TelegramClient] = None
        self._active = False
        self.signals_copied_today = 0

        # Daily cap — adjustable at runtime from the dashboard / NOVI.
        # 0 means unlimited.
        self.max_signals_per_day = 0

        # Cached resolved entities (avoids repeated lookups)
        self._target_entity = None
        self._source_entities: List[Any] = []
        self._handlers_registered = False

        # Recently copied signal fingerprints, to avoid double-posting the
        # same signal when a source channel edits or reposts it.
        self._recent_hashes: deque = deque(maxlen=200)

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def status(self) -> Dict:
        return {
            "active": self._active,
            "signals_copied_today": self.signals_copied_today,
            "max_signals_per_day": self.max_signals_per_day,  # 0 = unlimited
            "source_channels": self.source_channels,
            "sources_listening": len(self._source_entities),
            "target_group": self.signal_target_group,
            "target_resolved": self._target_entity is not None,
            "connected": bool(self.client and self.client.is_connected()),
        }

    def set_daily_limit(self, new_limit: int):
        """Adjusts the max signals copied per day. 0 = unlimited."""
        self.max_signals_per_day = max(0, int(new_limit))
        logger.info(
            f"Signal Copier daily limit set to "
            f"{'unlimited' if self.max_signals_per_day == 0 else self.max_signals_per_day}."
        )

    async def connect(self):
        if not self.api_id or not self.api_hash:
            logger.warning("Signal Copier credentials missing.")
            return

        if not self.source_channels:
            logger.warning("No source signal channels configured. Signal Copier will not start.")
            return

        # Reconnect path: if a client already exists, just bring the socket back
        # up. Re-running full setup would register a SECOND event handler and
        # cause every signal to be posted twice.
        if self.client is not None and self._handlers_registered:
            try:
                if not self.client.is_connected():
                    await self.client.connect()
                if await self.client.is_user_authorized():
                    logger.info("Signal Copier reconnected (existing handlers reused).")
                    return
                logger.error("Signal Copier session is no longer authorized.")
                return
            except Exception as e:
                logger.error(f"Signal Copier reconnect failed: {type(e).__name__}: {e}")
                return

        try:
            self.client = TelegramClient(StringSession(self.session_string), self.api_id, self.api_hash)
            await self.client.start(phone=self.phone)  # type: ignore

            me = await self.client.get_me()
            logger.info(f"Signal Copier connected as {me.first_name}.")  # type: ignore

            # Auto-join source channels and resolve each to a concrete entity.
            # Passing resolved entities (rather than raw strings) to the event
            # filter is essential: if Telethon cannot resolve a name at filter
            # build time it silently matches NOTHING, which is why no signals
            # were ever copied.
            resolved_sources = []
            for channel in self.source_channels:
                try:
                    entity = await resolve_chat(self.client, channel, "signal source")
                    if entity is None:
                        # Not joined yet — join, then resolve again
                        await self.client(JoinChannelRequest(channel))
                        await asyncio.sleep(random.uniform(2, 5))
                        entity = await resolve_chat(self.client, channel, "signal source")

                    if entity is not None:
                        try:
                            await self.client(JoinChannelRequest(entity))
                        except Exception:
                            pass  # already a member — fine
                        resolved_sources.append(entity)
                        self._source_entities.append(entity)
                        logger.info(f"Signal Copier listening to {channel}")
                    else:
                        logger.error(f"Could not resolve source channel {channel} — it will be skipped.")
                    await asyncio.sleep(random.uniform(2, 5))
                except Exception as e:
                    logger.warning(f"Could not join source channel {channel}: {type(e).__name__}: {e}")

            if not resolved_sources:
                logger.error("No source channels could be resolved. Signal Copier cannot listen.")
                if self.nm:
                    await self.nm.send_notification(
                        subject="Signal Copier: No Source Channels Reachable",
                        message=(
                            f"None of the configured source channels could be resolved:\n"
                            f"{', '.join(self.source_channels)}\n\n"
                            f"The Signal Copier is connected but will not receive any signals."
                        ),
                        is_critical=True
                    )
                return

            # Register real-time event listener against resolved entities
            @self.client.on(events.NewMessage(chats=resolved_sources))
            async def new_signal_handler(event):
                if self._active:
                    await self._handle_new_signal(event)

            self._handlers_registered = True

            # Verify the target group is reachable before declaring ourselves ready
            target = await self._get_target_entity()
            if target is None:
                logger.error(
                    f"Signal target group '{self.signal_target_group}' is NOT reachable. "
                    f"Signals will be cleansed but cannot be delivered."
                )

            self._active = True
            logger.info(
                f"Signal Copier is ACTIVE, listening to {len(resolved_sources)} channel(s) "
                f"-> posting to '{getattr(target, 'title', self.signal_target_group)}'."
            )

            # Notify
            if self.nm:
                await self.nm.notify_module_status(
                    "Signal Copier (Whale Tracker VIP)", "Active",
                    f"Listening to {', '.join(self.source_channels)}. Posting to group {self.signal_target_group}."
                )

        except Exception as e:
            logger.error(f"Failed to connect Signal Copier: {e}")
            self.client = None

    async def activate(self):
        """Activate signal copying from dashboard."""
        self._active = True
        logger.info("Signal Copier ACTIVATED from dashboard.")
        if self.nm:
            await self.nm.notify_module_status("Signal Copier (Whale Tracker VIP)", "Active",
                                               "Signal copying has been turned ON from the dashboard.")
        if self.db:
            await self.db.log_metric("signal_copier_status", 1, {"action": "activated"})

    async def deactivate(self):
        """Deactivate signal copying from dashboard."""
        self._active = False
        logger.info("Signal Copier DEACTIVATED from dashboard.")
        if self.nm:
            await self.nm.notify_module_status("Signal Copier (Whale Tracker VIP)", "Deactivated",
                                               "Signal copying has been turned OFF from the dashboard.")
        if self.db:
            await self.db.log_metric("signal_copier_status", 0, {"action": "deactivated"})

    async def _handle_new_signal(self, event):
        """Processes an incoming signal in real-time."""
        raw_text = event.message.message
        if not raw_text or len(raw_text) < 20:
            return

        # Respect the daily cap (0 = unlimited)
        if self.max_signals_per_day and self.signals_copied_today >= self.max_signals_per_day:
            logger.info(
                f"Daily signal limit reached "
                f"({self.signals_copied_today}/{self.max_signals_per_day}). Skipping."
            )
            return

        # Skip duplicates (edited/reposted signals)
        fingerprint = hashlib.md5(raw_text.strip().encode("utf-8", "ignore")).hexdigest()
        if fingerprint in self._recent_hashes:
            logger.info("Duplicate signal detected. Skipping.")
            return
        self._recent_hashes.append(fingerprint)

        logger.info("New Signal Detected in source channel! Processing...")

        # 1. Cleanse with AI
        cleansed_text = await self._cleanse_signal(raw_text)
        if not cleansed_text or cleansed_text.strip().upper() == "REJECT":
            logger.info("Signal rejected (locked VIP teaser or non-signal post). Dropping.")
            return

        # 2. Extract image if any
        image_path = None
        if event.message.media:
            try:
                temp_dir = tempfile.gettempdir()
                filename = f"signal_{event.message.id}.jpg"
                image_path = os.path.join(temp_dir, filename)
                await event.message.download_media(file=image_path)
                logger.info("Downloaded chart/image for signal.")
            except Exception as e:
                logger.error(f"Failed to download signal media: {e}")
                image_path = None

        # 3. Random human delay (1 to 2 minutes)
        delay = random.uniform(60, 120)
        logger.info(f"Signal cleansed. Waiting {delay:.0f}s before posting...")
        await asyncio.sleep(delay)

        # 4. Post directly to the target group using our own Telethon client
        success = await self._post_to_group(cleansed_text, image_path)

        if success:
            self.signals_copied_today += 1
            logger.info(f"Successfully copied signal! (Total today: {self.signals_copied_today})")

            # Email notification
            if self.nm:
                await self.nm.send_notification(
                    subject="New Crypto Signal Posted to Whale Tracker VIP!",
                    message=f"A new signal was copied, cleansed by AI, and posted to your group.\n\n"
                            f"Signals posted today: {self.signals_copied_today}\n\n"
                            f"Preview:\n{cleansed_text[:300]}",
                    is_critical=False
                )

            # Supabase
            if self.db:
                await self.db.log_post(
                    platform="whale_tracker_vip",
                    content=cleansed_text,
                    image_path=image_path or "",
                    status="posted",
                    metadata={"type": "crypto_signal", "signals_today": self.signals_copied_today}
                )

        # Clean up temp image
        if image_path and os.path.exists(image_path):
            try:
                os.remove(image_path)
            except Exception:
                pass

    async def _post_to_group(self, text: str, image_path: Optional[str] = None) -> bool:
        """
        Posts the cleansed signal directly to the target group.

        Uses the shared resolver so a group id written as -5533411583 (missing
        the -100 supergroup prefix) still works. The resolved entity is cached
        so we only pay the lookup cost once.
        """
        if not self.client or not self.signal_target_group:
            logger.error("Cannot post: client not connected or no target group configured.")
            return False

        try:
            entity = await self._get_target_entity()
            if entity is None:
                logger.error(
                    f"Signal target group '{self.signal_target_group}' could not be resolved. "
                    f"Make sure the account is a MEMBER of that group and the ID is correct."
                )
                if self.nm:
                    await self.nm.send_notification(
                        subject="Signal Copier: Target Group Unreachable",
                        message=(
                            f"Could not resolve the Whale Tracker target group "
                            f"'{self.signal_target_group}'.\n\n"
                            f"Check that the burner account is a member of the group and that "
                            f"SIGNAL_TARGET_GROUP is correct (supergroups look like -100XXXXXXXXXX)."
                        ),
                        is_critical=True
                    )
                return False

            if image_path and os.path.exists(image_path):
                await self.client.send_file(
                    entity, file=image_path, caption=text[:1024], parse_mode="html"
                )
            else:
                await self.client.send_message(entity, message=text, parse_mode="html")

            logger.info(f"Signal posted to group: {getattr(entity, 'title', 'Unknown')}")
            return True

        except FloodWaitError as e:
            logger.warning(f"FloodWait! Backing off {e.seconds}s.")
            await asyncio.sleep(e.seconds)
            return False
        except Exception as e:
            logger.error(f"Failed to post signal to group: {type(e).__name__}: {e}")
            # Drop the cached entity so the next attempt re-resolves it
            self._target_entity = None
            return False

    async def _get_target_entity(self):
        """Resolves and caches the target group entity."""
        if self._target_entity is not None:
            return self._target_entity
        self._target_entity = await resolve_chat(
            self.client, self.signal_target_group, "signal target group"
        )
        return self._target_entity

    async def _cleanse_signal(self, raw_text: str) -> Optional[str]:
        """Uses AI to strip competitor links/branding and format it for our brand."""
        system_prompt = f"""You are a professional Crypto Signal Editor for the Whale Tracker VIP group.
You will be given a raw trading signal from another group.

YOUR JOB:
1. First, check if the signal is a "locked" VIP teaser. A locked teaser has:
   - Lock emojis (🔐🔒) hiding the actual numbers
   - Text like "Details Available on VIP Channel" or "Buy VIP Subscription"
   - Missing actual entry prices, targets, or stop loss numbers
   If it IS a locked teaser, output exactly and ONLY the word: REJECT

2. Also REJECT if the message is just an advertisement, promo, or non-signal content (e.g., "Join our group", profit updates, general market commentary without specific trade data).

3. If it is a valid free signal with real entry prices and targets:
   - Extract the core trading data (Coin pair, Direction, Leverage, Entry zone, Take Profits, Stop Loss).
   - COMPLETELY REMOVE any mentions of the competitor's channel name, links, @usernames, or VIP group upsells.
   - Keep the exact numbers/prices identical. Do not change the financial data.
   - Add nice emojis for readability.
   - At the very bottom, always append exactly:
   "Powered by {self.channel_username}"

Do not add any conversational filler. Just output the final signal text (or REJECT)."""

        user_prompt = f"RAW SIGNAL TO CLEANSE:\n\n{raw_text}"

        try:
            cleansed = await self.ai.generate(
                task="signal_cleansing",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=500,
                temperature=0.2
            )
            return cleansed
        except Exception as e:
            logger.error(f"AI cleansing failed: {e}")
            return None

    async def disconnect(self):
        self._active = False
        if self.client:
            await self.client.disconnect()
            logger.info("Signal Copier disconnected.")
