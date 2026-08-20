"""
Buffer Broadcaster — Facebook (and any other Buffer-connected channel).

Uses Buffer's GraphQL API at https://api.buffer.com/graphql.

Notes learned from probing the live API (the legacy REST API at
api.bufferapp.com rejects modern public tokens and retires 2027-02-01):

  * `channels` is a TOP-LEVEL query and needs an organizationId.
  * `createPost` returns the union `PostActionPayload`, so every response
    must be read through `__typename` — a failure comes back as HTTP 200
    with an error variant, never as an HTTP error status.
  * Facebook posts REQUIRE `metadata.facebook.type` ("post" | "reel" | "story").
    Omitting it fails with "Facebook posts require a type".
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("OmniBot.Buffer")

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


class BufferBroadcaster:
    """Publishes to Facebook (and other channels) through Buffer."""

    # Services needing an explicit post type in metadata
    _TYPED_SERVICES = {"facebook": "post", "instagram": "post"}

    def __init__(self, access_token: str, db=None, ai_engine=None, brain=None,
                 organization_id: str = "", enabled_services: Optional[List[str]] = None,
                 site_url: str = ""):
        self.token = (access_token or "").strip()
        self.db = db
        self.ai = ai_engine
        self.brain = brain
        self.organization_id = (organization_id or "").strip()
        self.site_url = (site_url or "").rstrip("/")
        # Which Buffer services we post to. Facebook only, by default.
        self.enabled_services = [s.lower() for s in (enabled_services or ["facebook"])]

        self.channels: List[Dict] = []
        self._connected = False
        self.posts_sent = 0
        self.last_error = ""

        if not self.token:
            logger.warning("Buffer access token missing. Facebook posting disabled.")

    @property
    def is_ready(self) -> bool:
        return self._connected and bool(self.target_channels)

    @property
    def target_channels(self) -> List[Dict]:
        return [c for c in self.channels
                if c.get("service", "").lower() in self.enabled_services
                and not c.get("isDisconnected")]

    @property
    def status(self) -> Dict:
        return {
            "configured": bool(self.token),
            "connected": self._connected,
            "organization_id": self.organization_id,
            "channels": [{"name": c.get("name"), "service": c.get("service"),
                          "connected": not c.get("isDisconnected")} for c in self.channels],
            "active_channels": len(self.target_channels),
            "posts_sent": self.posts_sent,
            "last_error": self.last_error,
        }

    # ── transport ────────────────────────────────────────────────

    async def _gql(self, query: str, variables: Dict = None,
                   timeout: int = 30) -> Optional[Dict]:
        """Runs a GraphQL request. Returns the `data` object, or None."""
        if not self.token:
            return None

        import aiohttp
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    BUFFER_GRAPHQL_URL, json=payload,
                    headers={"Authorization": f"Bearer {self.token}",
                             "Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    body = await resp.json(content_type=None)

                    if resp.status == 401:
                        self.last_error = "Buffer token rejected (401)."
                        logger.error(self.last_error)
                        return None

                    if body.get("errors"):
                        msg = body["errors"][0].get("message", "unknown")
                        self.last_error = f"Buffer GraphQL error: {msg}"
                        logger.error(self.last_error)
                        # Partial data can still be usable
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
        """Verifies the token and loads the connected channels."""
        if not self.token:
            return False

        if not self.organization_id:
            data = await self._gql(_ACCOUNT)
            account = (data or {}).get("account") or {}
            orgs = account.get("organizations") or []
            if not orgs:
                self.last_error = "Buffer account has no organizations."
                logger.error(self.last_error)
                return False
            self.organization_id = orgs[0]["id"]
            logger.info(f"Buffer connected as {account.get('email')} "
                        f"(org: {orgs[0].get('name')})")

        data = await self._gql(_CHANNELS, {"i": {"organizationId": self.organization_id}})
        self.channels = ((data or {}).get("channels") or [])

        if not self.channels:
            self.last_error = ("No channels connected in Buffer. "
                               "Connect your Facebook page at buffer.com.")
            logger.warning(self.last_error)
            self._connected = bool(self.organization_id)
            return False

        self._connected = True
        for c in self.channels:
            state = "disconnected" if c.get("isDisconnected") else "ready"
            logger.info(f"Buffer channel: {c.get('service')} / {c.get('name')} [{state}]")

        if not self.target_channels:
            logger.warning(f"No enabled Buffer channels for services: {self.enabled_services}")
        return True

    # ── posting ──────────────────────────────────────────────────

    async def post(self, package: Dict, article_slug: str = "") -> bool:
        """
        Publishes a content package to every enabled Buffer channel.
        Returns True if at least one channel accepted the post.
        """
        if not self.token:
            return False

        if not self._connected:
            await self.connect()
        if not self.target_channels:
            logger.warning("Buffer: no connected channel to post to.")
            return False

        text = await self._build_caption(package, article_slug)
        if not text:
            logger.error("Buffer: empty caption, aborting.")
            return False

        image_url = self._pick_image(package)

        any_ok = False
        for channel in self.target_channels:
            ok = await self._post_to_channel(channel, text, image_url, article_slug)
            any_ok = any_ok or ok
            await asyncio.sleep(1)  # be gentle with the API

        return any_ok

    async def _post_to_channel(self, channel: Dict, text: str,
                               image_url: str, article_slug: str) -> bool:
        service = (channel.get("service") or "").lower()

        # Buffer downloads the picture itself, so this has to be a public URL —
        # the local file Telegram uploads is no use here. `assets` was
        # previously left empty, which is why every Facebook post went out
        # without an image even though one had been generated.
        assets: List[Dict[str, Any]] = []
        if image_url and image_url.startswith("http"):
            assets.append({"image": {"url": image_url, "thumbnailUrl": image_url}})

        post_input: Dict[str, Any] = {
            "channelId": channel["id"],
            "text": text,
            "assets": assets,
            "mode": "addToQueue",          # respects your Buffer schedule
            "schedulingType": "automatic",  # Buffer publishes it for us
            "needsApproval": False,
            "saveToDraft": False,
            "aiAssisted": True,
            "source": "novi-bot",
        }

        # Facebook/Instagram reject posts without an explicit type
        if service in self._TYPED_SERVICES:
            post_input["metadata"] = {service: {"type": self._TYPED_SERVICES[service]}}

        if not assets:
            logger.warning(f"Buffer: posting to {service} without an image "
                           f"(no public URL available for this story).")

        data = await self._gql(_CREATE_POST, {"i": post_input})
        result = (data or {}).get("createPost") or {}
        kind = result.get("__typename")

        if kind == "PostActionSuccess":
            post = result.get("post") or {}
            self.posts_sent += 1
            self.last_error = ""
            logger.info(f"Buffer: queued {service} post "
                        f"({channel.get('name')}) id={post.get('id')} status={post.get('status')}")
            if self.db:
                await self.db.log_social_post(
                    platform=service, provider="buffer", content=text,
                    image_url=image_url, status="sent",
                    external_id=post.get("id", ""), article_slug=article_slug)
                await self.db.log_post(
                    platform=service, content=text, image_path=image_url,
                    status="posted", metadata={"provider": "buffer",
                                               "channel": channel.get("name")})
            return True

        # Every failure arrives as an HTTP 200 union variant
        message = result.get("message") or self.last_error or f"unexpected response: {kind}"
        self.last_error = message
        logger.error(f"Buffer: {service} post failed — {message}")
        if self.db:
            await self.db.log_social_post(
                platform=service, provider="buffer", content=text,
                image_url=image_url, status="failed", error=message,
                article_slug=article_slug)
            await self.db.log_error("BufferBroadcaster", kind or "PostFailed",
                                    message, auto_resolved=False)
        return False

    # ── content helpers ──────────────────────────────────────────

    @staticmethod
    def _pick_image(package: Dict) -> str:
        """
        The picture Buffer should fetch.

        Our own generated-and-hosted image first, since that is what went out
        on Telegram and keeps the story looking the same everywhere; the
        outlet's photo only if we have nothing hosted.
        """
        return package.get("image_url") or package.get("real_image_url") or ""

    def _article_link(self, article_slug: str) -> str:
        # The site serves articles at the root: /{slug}
        if article_slug and self.site_url:
            return f"{self.site_url}/{article_slug}"
        return ""

    async def _build_caption(self, package: Dict, article_slug: str = "") -> str:
        """
        Builds a Facebook-appropriate caption.

        Telegram copy is short and emoji-dense; Facebook rewards a little more
        context, so we ask the AI to adapt it and fall back to the original.
        """
        base = (package.get("telegram_text") or package.get("tweet_text") or "").strip()
        if not base:
            return ""

        link = self._article_link(article_slug)
        caption = base

        if self.ai:
            try:
                rewritten = await self.ai.generate(
                    task="social_caption",
                    system_prompt=(
                        "You adapt short news posts into Facebook captions. "
                        "Keep every fact identical. Write 2-4 short paragraphs, "
                        "friendly and readable, keep a few relevant emojis, and end "
                        "with 3-5 relevant hashtags. Output only the caption."
                    ),
                    user_prompt=f"Adapt this for Facebook:\n\n{base}",
                    max_tokens=420,
                    temperature=0.7,
                )
                # Only accept a rewrite that is actually a caption. The
                # engine already rejects narration, but the Telegram copy is a
                # perfectly good caption, so anything doubtful falls back to it
                # rather than risking the model's deliberation on the page.
                if rewritten and 40 < len(rewritten.strip()) < 2200:
                    caption = rewritten.strip()
                elif rewritten:
                    logger.warning("Caption rewrite was not usable; keeping the "
                                   "original post text.")
            except Exception as e:
                logger.warning(f"Caption rewrite failed, using original: {type(e).__name__}")

        if link:
            caption = f"{caption}\n\n📖 Read the full story: {link}"

        # Facebook's hard limit is ~63k, but long captions get truncated in-feed
        return caption[:5000]
