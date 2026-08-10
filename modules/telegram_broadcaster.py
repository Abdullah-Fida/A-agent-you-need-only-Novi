"""
Telegram Broadcaster Module.
Pushes generated content packages (image + text) directly to the Telegram channel
via the official Telegram Bot API.

Uses python-telegram-bot for reliable delivery with built-in FloodWait protection.
"""
import logging
import asyncio
import os
import re
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
        self.posts_without_image = 0

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

        # Why the post ended up with or without a photo. Recorded verbatim so
        # the database answers "did this post carry an image?" truthfully —
        # it previously stored the intended path on both branches, which made
        # a text-only post indistinguishable from an illustrated one.
        image_status = self._classify_image(image_path)
        sent_with_image = False

        try:
            if image_status == "ok":
                sent_with_image = await self._send_photo_with_caption(image_path, telegram_text)
                if not sent_with_image:
                    image_status = "upload_failed"

            if not sent_with_image:
                logger.warning(f"Posting without an image (reason: {image_status}).")
                await self._send_text_only(telegram_text)

            self.posts_sent += 1
            logger.info(f"Successfully broadcast post #{self.posts_sent} to "
                        f"{self.channel_username} (image: "
                        f"{'attached' if sent_with_image else 'MISSING — ' + image_status}).")

            # Update Brain subscriber count
            sub_count = await self.get_subscriber_count()
            if self.brain:
                self.brain.current_subscribers = sub_count

            if self.db:
                await self.db.log_post(
                    platform="telegram_channel",
                    content=telegram_text,
                    # Only a genuinely delivered photo is recorded here.
                    image_path=image_path if sent_with_image else "",
                    status="posted",
                    metadata={"category": package.get("category", ""),
                              "source_credits": package.get("source_credits", ""),
                              "has_image": sent_with_image,
                              "image_status": image_status,
                              "image_source": package.get("image_source", "")}
                )

            # An image is a hard requirement for this channel, so a post that
            # goes out without one is an incident, not a detail in the log.
            if not sent_with_image:
                self.posts_without_image += 1
                await self._alert_missing_image(package, image_status)

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

    def _classify_image(self, image_path: str) -> str:
        """Why this post can or cannot carry a photo — one of the status strings."""
        if not image_path:
            return "not_generated"
        if not os.path.exists(image_path):
            return "file_missing"
        size = os.path.getsize(image_path)
        if size == 0:
            return "file_empty"
        if size > self.MAX_PHOTO_SIZE_BYTES:
            return "too_large"
        return "ok"

    async def _send_photo_with_caption(self, image_path: str, caption: str) -> bool:
        """
        Sends the photo, retrying once. Returns whether the photo went out.

        A failure here must not fall through to the generic handler: the caller
        still needs to deliver the text, so this reports rather than raises.
        """
        caption = self._truncate_html(caption, 1024)

        for attempt in (1, 2):
            try:
                await self.client.send_file(
                    self.channel_username,
                    file=image_path,
                    caption=caption,
                    parse_mode="HTML",
                )
                logger.info("Photo + caption sent successfully.")
                return True
            except FloodWaitError:
                raise           # handled by the caller's back-off
            except Exception as e:
                logger.warning(f"Photo upload attempt {attempt} failed: "
                               f"{type(e).__name__}: {e}")
                if attempt == 1:
                    await asyncio.sleep(3)
        return False

    async def _send_text_only(self, text: str):
        await self.client.send_message(
            self.channel_username,
            message=self._truncate_html(text, 4096),
            parse_mode="HTML",
        )
        logger.info("Text-only message sent successfully.")

    @staticmethod
    def _truncate_html(text: str, limit: int) -> str:
        """
        Shortens text to Telegram's limit without breaking the HTML it parses.

        A naive slice can land inside a tag or leave <b> unclosed, and Telegram
        rejects the whole message when that happens — which previously turned an
        over-long post into a total send failure rather than a shortened one.
        """
        if len(text) <= limit:
            return text

        ellipsis = "…"
        cut = limit - len(ellipsis)

        # Never end inside a tag
        last_open = text.rfind("<", 0, cut)
        if last_open != -1 and text.find(">", last_open) >= cut:
            cut = last_open

        # Prefer a paragraph or sentence boundary if one is close by
        for boundary in ("\n", ". ", " "):
            found = text.rfind(boundary, int(cut * 0.7), cut)
            if found != -1:
                cut = found
                break

        clipped = text[:cut].rstrip()

        # Close whatever formatting is still open
        opened: list = []
        for match in re.finditer(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)[^>]*>", clipped):
            closing, tag = match.group(1), match.group(2).lower()
            if tag == "br":
                continue
            if closing:
                if opened and opened[-1] == tag:
                    opened.pop()
                elif tag in opened:
                    opened.remove(tag)
            else:
                opened.append(tag)

        return clipped + ellipsis + "".join(f"</{tag}>" for tag in reversed(opened))

    async def _alert_missing_image(self, package: Dict, reason: str):
        """Raises an alert when a post ships without its image."""
        title = package.get("original_title", "News Post")
        detail = (
            f"A post was published to {self.channel_username} WITHOUT an image.\n\n"
            f"Headline: {title}\n"
            f"Category: {package.get('category', 'N/A')}\n"
            f"Reason: {reason}\n"
            f"Path expected: {package.get('image_path') or '(none produced)'}\n\n"
            f"Posts without an image so far this run: {self.posts_without_image}"
        )
        logger.error(detail.replace("\n", " | "))

        if self.db:
            await self.db.log_error(
                module="TelegramBroadcaster",
                error_type="PostedWithoutImage",
                error_message=f"{reason} — {title[:120]}",
                auto_resolved=False,
            )
        if self.nm:
            await self.nm.send_notification(
                subject="Post published without an image",
                message=detail,
                is_critical=True,
            )

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
