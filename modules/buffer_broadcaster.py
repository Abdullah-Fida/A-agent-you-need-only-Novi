"""
Buffer transport — Facebook, X/Twitter and Threads.

Uses Buffer's GraphQL API at https://api.buffer.com/graphql.

This module is only the wire: it knows how to talk to Buffer and how each
service wants a post shaped. WHAT to say and WHEN to say it belongs to
`modules/social_syndicator.py`, which drives this off the article agent.

Notes learned from probing the live API (the legacy REST API at
api.bufferapp.com rejects modern public tokens and retires 2027-02-01):

  * `channels` is a TOP-LEVEL query and needs an organizationId.
  * `createPost` returns the union `PostActionPayload`, so every response
    must be read through `__typename` — a failure comes back as HTTP 200
    with an error variant, never as an HTTP error status.
  * Facebook posts REQUIRE `metadata.facebook.type` ("post" | "reel" | "story").
    Omitting it fails with "Facebook posts require a type".
  * `mode` is the ShareMode enum: addToQueue | customScheduled | shareNext |
    shareNow. We use `shareNow`, for two reasons. The post has to go out at
    the moment its article publishes, which is the whole design; and the free
    plan allows only TEN posts to sit in the queue at once. That limit is
    `OrganizationLimits.scheduledPosts` -- "the maximum number of scheduled
    posts allowed for the ORGANIZATION", so it is ten across every channel
    together, not ten each. Six posts a day on two channels is twelve, which
    fills it before the first day is out; the previous account was found
    already jammed at "10 scheduled posts out of 10 allowed" and silently
    refusing everything. Nothing published immediately ever enters that
    queue, so the limit does not apply to us at all.
  * X/Twitter needs no such metadata, but its text is hard-capped at 280
    characters and Buffer rejects anything longer outright.
  * Threads needs no metadata either -- every field on
    ThreadsPostMetadataInput is optional, checked against the live schema --
    but its limit is 500 characters and it charges a URL at its real length,
    so it gets its own caption rather than a truncated Facebook one.

REQUEST BUDGET
--------------
Buffer's API allows 100 requests per 15 minutes, 500 per 24 hours and 10,000
per 30 days, shared across every key and integration on the account. The
live responses carry it as `ratelimit: "100-in-15min"`.

A full day of publishing costs about twenty requests: two at start-up
(account, then channels) and one per post. Six posts on three channels --
Facebook, X and Threads -- is eighteen. That is 4% of the daily allowance,
and the largest burst in any fifteen minutes is three: one article, one
request per channel.

The one thing that moves the number is running with a service enabled whose
channel is not connected: `ensure_channels` then rechecks, at most once per
REFRESH_AFTER_SECONDS, which over eight publishing slots adds sixteen. Still
under 5%. A test asserts the whole budget so a future change that starts
polling cannot quietly eat it.
"""
import asyncio
import logging
import time
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
    """Publishes to Facebook, X/Twitter and Threads through Buffer."""

    # Services needing an explicit post type in metadata
    _TYPED_SERVICES = {"facebook": "post", "instagram": "post"}

    # Buffer still calls the X channel "twitter" in its API. Accept the new
    # name too, so a rename on their side does not silently drop the channel.
    _SERVICE_ALIASES = {"x": "twitter", "twitter": "twitter"}

    # How long to wait before asking Buffer again about a service whose
    # channel we cannot see.
    REFRESH_AFTER_SECONDS = 1800

    def __init__(self, access_token: str, db=None,
                 organization_id: str = "",
                 enabled_services: Optional[List[str]] = None):
        self.token = (access_token or "").strip()
        self.db = db
        self.organization_id = (organization_id or "").strip()
        # Which Buffer services we post to.
        self.enabled_services = [
            self.canonical_service(s)
            for s in (enabled_services or ["facebook", "twitter", "threads"])]

        self.channels: List[Dict] = []
        self._connected = False
        self._loaded_at = 0.0
        # Which Buffer login these channels came from. Shown in /api/health so
        # a token swap is visible without opening buffer.com.
        self.account_email = ""
        self.posts_sent = 0
        self.last_error = ""

        if not self.token:
            logger.warning("Buffer access token missing. Facebook and X "
                           "posting are disabled.")

    @property
    def is_ready(self) -> bool:
        return self._connected and bool(self.target_channels)

    @classmethod
    def canonical_service(cls, service: str) -> str:
        """'X' and 'twitter' are the same channel. Everything else is itself."""
        name = (service or "").strip().lower()
        return cls._SERVICE_ALIASES.get(name, name)

    @property
    def target_channels(self) -> List[Dict]:
        return [c for c in self.channels
                if self.canonical_service(c.get("service", "")) in self.enabled_services
                and not c.get("isDisconnected")]

    def channels_for(self, service: str) -> List[Dict]:
        """Every live channel for one service, e.g. all Facebook pages."""
        want = self.canonical_service(service)
        return [c for c in self.target_channels
                if self.canonical_service(c.get("service", "")) == want]

    async def ensure_channels(self, service: str) -> List[Dict]:
        """
        Channels for a service, re-reading Buffer first if we have none.

        The list is loaded once at start-up, which is wrong the moment a
        channel is connected at buffer.com afterwards: a brand-new Facebook
        page or X account would stay invisible to a running bot until the
        next deploy, with nothing in the log to say why. Rechecking costs one
        request, and only when a service currently looks absent.
        """
        found = self.channels_for(service)
        if found or not self.token:
            return found
        if time.monotonic() - self._loaded_at < self.REFRESH_AFTER_SECONDS:
            return found
        logger.info(f"No {service} channel on file — re-reading Buffer in case "
                    f"one was connected since start-up.")
        # Stamped even if the reload fails, so a permanently absent channel
        # cannot turn every article into another round-trip.
        self._loaded_at = time.monotonic()
        try:
            await self.connect()
        except Exception as e:
            logger.warning(f"Buffer channel refresh failed: {type(e).__name__}: {e}")
        return self.channels_for(service)

    @property
    def status(self) -> Dict:
        return {
            "configured": bool(self.token),
            "connected": self._connected,
            "account": self.account_email,
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
        """
        Verifies the token and loads the connected channels.

        The organisation is ALWAYS read from the account the token belongs
        to, and a configured BUFFER_ORGANIZATION_ID is only honoured if that
        account actually owns it. Swapping to a different Buffer account
        means a new token beside an organisation id left over from the old
        one, and the old code trusted that id blindly: the query then asked a
        stranger's organisation for its channels, got nothing back, and said
        only "No channels connected in Buffer" -- which sends you looking at
        buffer.com, where everything is plainly connected.
        """
        if not self.token:
            return False

        data = await self._gql(_ACCOUNT)
        account = (data or {}).get("account") or {}
        orgs = account.get("organizations") or []

        if orgs:
            owned = {o["id"] for o in orgs}
            if self.organization_id and self.organization_id not in owned:
                logger.warning(
                    f"BUFFER_ORGANIZATION_ID={self.organization_id} does not "
                    f"belong to {account.get('email')}. It is left over from "
                    f"another Buffer account; using {orgs[0]['id']} instead. "
                    f"Update or remove the variable.")
                self.organization_id = ""
            if not self.organization_id:
                self.organization_id = orgs[0]["id"]
            self.account_email = account.get("email", "")
            logger.info(f"Buffer connected as {self.account_email} "
                        f"(org: {orgs[0].get('name')})")
        elif not self.organization_id:
            self.last_error = ("Buffer token rejected, or the account has no "
                               "organisation.")
            logger.error(self.last_error)
            return False

        data = await self._gql(_CHANNELS, {"i": {"organizationId": self.organization_id}})
        self.channels = ((data or {}).get("channels") or [])
        self._loaded_at = time.monotonic()

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

    # X wraps every link in t.co, which always counts as this many characters
    # no matter how long the real URL is.
    X_LINK_LENGTH = 23
    X_MAX_CHARS = 280

    # Publish on the spot. See the module docstring for why queueing is not
    # an option: it is both the wrong time and a cap of ten across the whole
    # organisation.
    PUBLISH_MODE = "shareNow"
    # Only if the account will not publish immediately. Queueing late beats
    # losing the post, and the queue-full error then says so in the log.
    FALLBACK_MODE = "addToQueue"

    async def send(self, channel: Dict, text: str, image_url: str = "",
                   article_slug: str = "") -> bool:
        """
        Queues one post on one Buffer channel.

        This is the whole public surface for publishing. Callers build the
        text; this decides how the request has to be shaped for the service.
        """
        if not self.token or not channel:
            return False

        service = self.canonical_service(channel.get("service", ""))

        if not (text or "").strip():
            logger.error(f"Buffer: refusing to queue an empty {service} post.")
            return False

        # Buffer downloads the picture itself, so this has to be a public URL —
        # a local file path is no use here. `assets` was previously left empty,
        # which is why every Facebook post went out without an image even
        # though one had been prepared.
        assets: List[Dict[str, Any]] = []
        if image_url and image_url.startswith("http"):
            assets.append({"image": {"url": image_url, "thumbnailUrl": image_url}})

        post_input: Dict[str, Any] = {
            "channelId": channel["id"],
            "text": text,
            "assets": assets,
            "mode": self.PUBLISH_MODE,      # straight out, not into the queue
            "schedulingType": "automatic",  # Buffer's workers send it
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

        # Some accounts refuse to publish on the spot. Queueing is the wrong
        # time and runs into the ten-post organisation cap within a day, but
        # it still beats dropping the post, so it is tried once and the
        # reason is logged either way.
        if kind not in ("PostActionSuccess", None) and self.FALLBACK_MODE:
            logger.warning(f"Buffer refused to publish to {service} "
                           f"immediately ({result.get('message')}); "
                           f"falling back to the queue.")
            post_input["mode"] = self.FALLBACK_MODE
            data = await self._gql(_CREATE_POST, {"i": post_input})
            result = (data or {}).get("createPost") or {}
            kind = result.get("__typename")

        if kind == "PostActionSuccess":
            post = result.get("post") or {}
            self.posts_sent += 1
            self.last_error = ""
            logger.info(f"Buffer: queued {service} post "
                        f"({channel.get('name')}) id={post.get('id')} "
                        f"status={post.get('status')}")
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
