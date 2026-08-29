"""
Pinterest publishing, through Buffer.

Buffer is an official Pinterest Marketing Partner, so posting through it needs
no Pinterest API approval of our own. Pinterest's own API grants Trial access
first, where pins are sandbox entities nobody but their creator can see, and
Standard access takes weeks of review — this route publishes real pins today
and stays inside Pinterest's terms, which browser automation would not.

Uses a SECOND Buffer account, separate from the one Novi posts Facebook with,
so the two do not share the free plan's channel and queue allowance.

The API details that matter, confirmed by introspecting the live schema:
  * `createPost` returns a union, so failures arrive as HTTP 200 with an error
    variant and must be read through `__typename`.
  * A pin's board, title and destination link go in
    `metadata.pinterest.{boardServiceId, title, url}`.
  * The image goes in `assets` as [{image: {url, thumbnailUrl}}] and Buffer
    fetches that URL itself, so it must be publicly reachable.
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("PinAgent.Publisher")

BUFFER_GRAPHQL_URL = "https://api.buffer.com/graphql"

_CREATE_POST = """
mutation($i: CreatePostInput!) {
  createPost(input: $i) {
    __typename
    ... on PostActionSuccess { post { id status } }
    ... on InvalidInputError   { message }
    ... on UnauthorizedError   { message }
    ... on LimitReachedError   { message }
    ... on NotFoundError       { message }
    ... on RestProxyError      { message }
    ... on UnexpectedError     { message }
  }
}
"""

_CHANNELS = """
query($i: ChannelsInput!) {
  channels(input: $i) { id name service isDisconnected isLocked }
}
"""

_ACCOUNT = "query { account { id email organizations { id name } } }"


class PinterestPublisher:
    """Queues pins to Pinterest through Buffer."""

    def __init__(self, access_token: str, organization_id: str = "",
                 board_id: str = "", db=None, max_queued: int = 8):
        self.token = (access_token or "").strip()
        self.organization_id = (organization_id or "").strip()
        self.board_id = (board_id or "").strip()
        self.db = db
        self.max_queued = max_queued

        self.channels: List[Dict] = []
        self._connected = False
        self.pins_sent = 0
        self.last_error = ""

        if not self.token:
            logger.warning("PIN_BUFFER_TOKEN is not set — Pinterest publishing "
                           "is disabled.")

    @property
    def is_ready(self) -> bool:
        return self._connected and bool(self.pinterest_channels)

    @property
    def pinterest_channels(self) -> List[Dict]:
        return [c for c in self.channels
                if (c.get("service") or "").lower() == "pinterest"
                and not c.get("isDisconnected")]

    @property
    def status(self) -> Dict:
        return {
            "configured": bool(self.token),
            "connected": self._connected,
            "board_id": self.board_id,
            "channels": [{"name": c.get("name"), "service": c.get("service"),
                          "connected": not c.get("isDisconnected")}
                         for c in self.channels],
            "pinterest_channels": len(self.pinterest_channels),
            "pins_sent": self.pins_sent,
            "last_error": self.last_error,
        }

    # ── transport ────────────────────────────────────────────────

    async def _gql(self, query: str, variables: Optional[Dict] = None,
                   timeout: int = 30) -> Optional[Dict]:
        if not self.token:
            return None

        import aiohttp
        payload: Dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    BUFFER_GRAPHQL_URL, json=payload,
                    headers={"Authorization": f"Bearer {self.token}",
                             "Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response:
                    body = await response.json(content_type=None)

                    if response.status == 401:
                        self.last_error = "Buffer token rejected (401)."
                        logger.error(self.last_error)
                        return None

                    if body.get("errors"):
                        message = body["errors"][0].get("message", "unknown")
                        self.last_error = f"Buffer GraphQL error: {message}"
                        logger.error(self.last_error)
                        return body.get("data")

                    return body.get("data")

        except asyncio.TimeoutError:
            self.last_error = "Buffer request timed out."
            logger.error(self.last_error)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.error(f"Buffer request failed: {self.last_error}")
        return None

    # ── connection ───────────────────────────────────────────────

    async def connect(self) -> bool:
        """Verifies the token and finds the Pinterest channel."""
        if not self.token:
            return False

        if not self.organization_id:
            data = await self._gql(_ACCOUNT)
            account = (data or {}).get("account") or {}
            orgs = account.get("organizations") or []
            if not orgs:
                self.last_error = "This Buffer account has no organizations."
                logger.error(self.last_error)
                return False
            self.organization_id = orgs[0]["id"]
            logger.info(f"Pinterest Buffer account: {account.get('email')} "
                        f"(org: {orgs[0].get('name')})")

        data = await self._gql(_CHANNELS,
                               {"i": {"organizationId": self.organization_id}})
        self.channels = (data or {}).get("channels") or []

        if not self.pinterest_channels:
            self.last_error = ("No Pinterest channel connected in Buffer. "
                               "Connect the Pinterest account at buffer.com.")
            logger.warning(self.last_error)
            self._connected = bool(self.organization_id)
            return False

        self._connected = True
        for channel in self.pinterest_channels:
            logger.info(f"Pinterest channel ready: {channel.get('name')}")
        return True

    # ── boards ───────────────────────────────────────────────────

    async def find_board(self, name_contains: str = "") -> Optional[str]:
        """
        Looks up a board id from the connected channel.

        Buffer exposes boards per channel; the exact field name has varied, so
        several shapes are tried and a failure is reported rather than raised —
        a pin can still publish to the account's default board.
        """
        if not self.pinterest_channels:
            return None
        channel_id = self.pinterest_channels[0]["id"]

        for field in ("boards", "pinterestBoards"):
            query = ("query($id: ChannelId!) { channel(input: {id: $id}) { "
                     f"{field} {{ id name }} }} }}")
            data = await self._gql(query.replace("}} }", "} }"), {"id": channel_id})
            boards = ((data or {}).get("channel") or {}).get(field) or []
            if not boards:
                continue
            for board in boards:
                if not name_contains or name_contains.lower() in \
                        (board.get("name") or "").lower():
                    logger.info(f"Using Pinterest board: {board.get('name')}")
                    return board.get("id")
        logger.info("Could not list boards from Buffer; the pin will use the "
                    "channel's default board.")
        return None

    # ── publishing ───────────────────────────────────────────────

    async def publish(self, pin: Dict) -> bool:
        """
        Queues one pin. `pin` must already have passed the compliance gate.

        Returns True only when Buffer accepted it.
        """
        if not self.token:
            return False
        if not self._connected:
            await self.connect()

        channels = self.pinterest_channels
        if not channels:
            logger.error("No Pinterest channel to publish to.")
            return False

        image_url = (pin.get("image_url") or "").strip()
        if not image_url.startswith("http"):
            # Buffer downloads the image itself, so a local path is useless
            # here — the same defect that published Facebook posts with no
            # picture for weeks.
            logger.error("Pin has no publicly reachable image URL; not publishing.")
            self.last_error = "image not hosted"
            return False

        metadata: Dict[str, Any] = {
            "title": (pin.get("title") or "")[:100],
            "url": pin.get("link") or "",
        }
        board_id = pin.get("board_id") or self.board_id
        if board_id:
            metadata["boardServiceId"] = board_id

        post_input: Dict[str, Any] = {
            "channelId": channels[0]["id"],
            "text": pin.get("description") or "",
            "assets": [{"image": {"url": image_url, "thumbnailUrl": image_url}}],
            "mode": "addToQueue",
            "schedulingType": "automatic",
            "needsApproval": False,
            "saveToDraft": False,
            "aiAssisted": True,
            "source": "novi-pin-agent",
            "metadata": {"pinterest": metadata},
        }

        data = await self._gql(_CREATE_POST, {"i": post_input})
        result = (data or {}).get("createPost") or {}
        kind = result.get("__typename")

        if kind == "PostActionSuccess":
            post = result.get("post") or {}
            self.pins_sent += 1
            self.last_error = ""
            logger.info(f"Pin queued: '{pin.get('title', '')[:44]}' "
                        f"id={post.get('id')} status={post.get('status')}")
            if self.db:
                await self.db.log_social_post(
                    platform="pinterest", provider="buffer",
                    content=pin.get("description", ""), image_url=image_url,
                    status="sent", external_id=post.get("id", ""),
                    article_slug=pin.get("product_id", ""))
            return True

        message = result.get("message") or self.last_error or f"unexpected: {kind}"
        self.last_error = message

        # The free plan holds ten queued posts per channel. That is a full
        # queue, not a failure, so it is logged as such and the pin is simply
        # retried on the next cycle.
        if kind == "LimitReachedError":
            logger.warning(f"Buffer queue is full ({message}). Will retry later.")
        else:
            logger.error(f"Pin failed: {message}")

        if self.db:
            await self.db.log_social_post(
                platform="pinterest", provider="buffer",
                content=pin.get("description", ""), image_url=image_url,
                status="failed", error=message,
                article_slug=pin.get("product_id", ""))
            await self.db.log_error("PinPublisher", kind or "PostFailed",
                                    message, auto_resolved=(kind == "LimitReachedError"))
        return False
