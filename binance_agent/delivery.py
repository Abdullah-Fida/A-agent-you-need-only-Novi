"""
Delivers a finished draft to a Telegram group, ready to copy.

Binance Square has NO posting API. Publishing is a manual paste, so the only
thing worth optimising is how fast that paste is -- which means the post must
arrive as one block with nothing to trim off it.

Telegram's <pre> renders a tap-to-copy block on mobile. The whole post comes
across in one tap; the numbers and the reasoning sit in a SEPARATE message so
they can never be copied by accident into the post itself.
"""
import asyncio
import logging
import os
from typing import Dict, Optional

logger = logging.getLogger("BinanceAgent.Delivery")


def _esc(text: str) -> str:
    """Telegram HTML: these three characters, and only these three."""
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


class DraftDelivery:
    """Sends drafts to a Telegram group using an existing Telethon client."""

    def __init__(self, client_owner=None, group: str = ""):
        # `client_owner` is any object holding a live `.client` -- the signal
        # copier or the broadcaster. Sharing the connected client avoids a
        # second Telegram login, which would look like a new device.
        self.client_owner = client_owner
        self.group = (group or "").strip()
        self._entity = None
        self.sent = 0
        self.last_error = ""

    @property
    def client(self):
        return getattr(self.client_owner, "client", None)

    @property
    def is_ready(self) -> bool:
        return bool(self.client and self.group)

    async def _resolve(self):
        if self._entity is not None:
            return self._entity
        try:
            from utils.telegram_utils import resolve_chat
            self._entity = await resolve_chat(self.client, self.group,
                                              "binance draft group")
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.error(f"Could not resolve the draft group: {self.last_error}")
        return self._entity

    async def send(self, draft: Dict, index: int = 1, total: int = 1) -> bool:
        """
        Two messages: the post to copy, then the working behind it.

        Separate on purpose. One message containing both means selecting the
        post by hand every time, and eventually pasting the workings into
        Binance by mistake.
        """
        if not self.is_ready:
            self.last_error = "no Telegram client or no group configured"
            logger.warning(f"Draft not delivered: {self.last_error}")
            return False

        entity = await self._resolve()
        if entity is None:
            logger.error("Draft group unreachable — is the account a member?")
            return False

        header = (f"<b>BINANCE SQUARE — draft {index} of {total}</b>\n"
                  f"<code>${_esc(draft['base'])}</code>  ·  "
                  f"tap the block to copy, paste into Square.")
        # A required photo credit rides INSIDE the copy block, so it is
        # pasted with the post rather than noticed separately and forgotten.
        # The picture is embedded in the card being posted, so the licence
        # obligation travels with it -- and the Wikimedia fallback returns
        # attributed licences far more often than Openverse did, which is
        # what turned this from theoretical into a real omission.
        body = draft["text"]
        credit = (draft.get("credit") or "").strip()
        if credit:
            body = f"{body}\n\n{credit}"
        post = f"<pre>{_esc(body)}</pre>"
        image = draft.get("image_path") or ""

        try:
            if image and os.path.exists(image):
                # The caption cap is 1024 characters and a post can exceed it,
                # so the picture goes first with a short caption and the
                # copy-block follows as its own message. Splitting also keeps
                # the tap-to-copy block clean of anything else.
                await self.client.send_file(
                    entity, file=image,
                    caption=f"{header}", parse_mode="html")
                await asyncio.sleep(1)
                await self.client.send_message(entity, message=post,
                                               parse_mode="html",
                                               link_preview=False)
            else:
                await self.client.send_message(
                    entity, message=f"{header}\n\n{post}",
                    parse_mode="html", link_preview=False)
            await asyncio.sleep(1)
            await self.client.send_message(
                entity,
                message=("<b>The numbers behind it</b>\n"
                         f"<code>{_esc(draft['facts'])}</code>\n\n"
                         f"<i>Score {draft['score']} · every figure above is "
                         f"read from Binance, none invented.</i>"),
                parse_mode="html", link_preview=False)
            self.sent += 1
            logger.info(f"Draft delivered: ${draft['base']}")
            return True
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.error(f"Could not deliver the draft: {self.last_error}")
            self._entity = None      # re-resolve next time
            return False

    async def announce(self, text: str) -> bool:
        """A one-line note to the same group — used when a run finds nothing."""
        if not self.is_ready:
            return False
        entity = await self._resolve()
        if entity is None:
            return False
        try:
            await self.client.send_message(entity, message=_esc(text),
                                           parse_mode="html", link_preview=False)
            return True
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return False

    @property
    def status(self) -> Dict:
        return {"ready": self.is_ready, "group": self.group,
                "delivered": self.sent, "last_error": self.last_error}
