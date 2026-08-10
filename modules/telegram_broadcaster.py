"""
Telegram Broadcaster Module.
Pushes generated content packages (image + text) directly to the Telegram channel
via the official Telegram Bot API.

Uses python-telegram-bot for reliable delivery with built-in FloodWait protection.
"""
import logging
import asyncio
import os
from typing import Optional, Dict

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

logger = logging.getLogger("OmniBot.Broadcaster")

class TelegramBroadcaster:
    MAX_PHOTO_SIZE_BYTES = 10 * 1024 * 1024

    def __init__(self, api_id: int, api_hash: str, session_string: str, channel_username: str, 
                 notification_manager=None, db=None, brain=None):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_string = session_string
        self.channel_username = channel_username
        self.nm = notification_manager
        self.db = db
        self.brain = brain
        self.client: Optional[TelegramClient] = None
        self._initialized = False
        self.posts_sent = 0

    async def connect(self):
        if not self.api_id or not self.api_hash or not self.session_string:
            logger.warning("Telegram API credentials or Session String missing. Broadcaster disabled.")
            return

        try:
            # Reuse the existing client on reconnect instead of building a new
            # one — rebuilding drops the entity cache and can trip Telegram's
            # "new login" heuristics.
            if self.client is None:
                self.client = TelegramClient(
                    StringSession(self.session_string), self.api_id, self.api_hash
                )

            if not self.client.is_connected():
                await self.client.connect()

            if not await self.client.is_user_authorized():
                logger.error("Session string is invalid or expired. Broadcaster not authorized.")
                self._initialized = False
                return

            me = await self.client.get_me()
            self._initialized = True
            logger.info(f"Telegram Broadcaster initialized as {me.first_name} (Telethon mode). Target: {self.channel_username}")
        except Exception as e:
            # Must clear the flag, otherwise post() keeps trying to send on a
            # dead client and every post silently fails.
            self._initialized = False
            logger.error(f"Failed to initialize Telegram Client: {e}")

    async def post(self, package: Dict) -> bool:
        if not self._initialized:
            logger.warning("Broadcaster not initialized. Skipping broadcast.")
            return False

        telegram_text = package.get("telegram_text", "")
        image_path = package.get("image_path", "")

        if not telegram_text:
            logger.error("Empty telegram_text in package. Aborting broadcast.")
            return False

        try:
            if image_path and os.path.exists(image_path):
                file_size = os.path.getsize(image_path)
                if file_size > self.MAX_PHOTO_SIZE_BYTES:
                    logger.warning(f"Image too large ({file_size / 1024 / 1024:.1f}MB). Sending text-only.")
                    await self._send_text_only(telegram_text)
                else:
                    await self._send_photo_with_caption(image_path, telegram_text)
            else:
                logger.info("No image found. Sending text-only post.")
                await self._send_text_only(telegram_text)

            self.posts_sent += 1
            logger.info(f"Successfully broadcast post #{self.posts_sent} to {self.channel_username}")

            # Update Brain subscriber count
            sub_count = await self.get_subscriber_count()
            if self.brain:
                self.brain.current_subscribers = sub_count

            if self.db:
                await self.db.log_post(
                    platform="telegram_channel",
                    content=telegram_text,
                    image_path=image_path or "",
                    status="posted",
                    metadata={"category": package.get("category", ""), 
                              "source_credits": package.get("source_credits", "")}
                )

            if self.nm:
                title = package.get("original_title", "News Post")
                await self.nm.notify_post_success(
                    title=title,
                    category=package.get('category', 'N/A'),
                    channel=self.channel_username,
                    posts_today=self.posts_sent,
                    total_max=6
                )
            return True

        except FloodWaitError as e:
            logger.warning(f"FloodWait detected! Backing off for {e.seconds} seconds.")
            await asyncio.sleep(e.seconds)
            return False
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Broadcast failed: {error_msg}")
            if self.nm:
                await self.nm.send_notification(
                    subject="Broadcast FAILED",
                    message=f"Failed to publish post to {self.channel_username}.\nError: {error_msg}",
                    is_critical=True
                )
            return False

    async def _send_photo_with_caption(self, image_path: str, caption: str):
        if len(caption) > 1024:
            caption = caption[:1020] + "..."

        await self.client.send_file(
            self.channel_username,
            file=image_path,
            caption=caption,
            parse_mode="HTML"
        )
        logger.info("Photo + caption sent successfully.")

    async def _send_text_only(self, text: str):
        if len(text) > 4096:
            text = text[:4092] + "..."

        await self.client.send_message(
            self.channel_username,
            message=text,
            parse_mode="HTML"
        )
        logger.info("Text-only message sent successfully.")

    async def get_subscriber_count(self) -> int:
        if not self._initialized or not self.channel_username:
            return 0
        try:
            entity = await self.client.get_entity(self.channel_username)
            count = getattr(entity, 'participants_count', 0)
            return count if count is not None else 0
        except Exception as e:
            logger.error(f"Failed to get subscriber count: {e}")
            return 0

    async def disconnect(self):
        if self.client:
            await self.client.disconnect()
            logger.info("Telegram Broadcaster shut down.")

    @property
    def is_ready(self) -> bool:
        return self._initialized
