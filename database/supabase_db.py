"""
Supabase Database Manager.

Every call runs the (synchronous) supabase-py client in a worker thread.
Calling it directly from async code blocked the whole event loop — freezing
Telegram listeners and the scheduler for the duration of each round-trip.

Also verifies the schema at startup, so a missing table is reported loudly
once instead of failing silently on every single write.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("OmniBot.Database")

# Every table the bot needs
REQUIRED_TABLES = [
    "posts", "alerts", "metrics", "error_logs",
    "articles", "scraped_users", "bot_state", "social_posts",
]

# Public storage bucket holding generated article hero images
IMAGE_BUCKET = "article-images"


class SupabaseDB:
    """All database access. Never blocks the event loop."""

    def __init__(self, url: str, key: str, required_tables: List[str] = None):
        self.url = url
        self.key = key
        # Which tables this connection expects. The pin agent runs against
        # its OWN project holding only pin_posts, and checking Novi's eight
        # there printed a MISSING TABLES banner on every start-up for tables
        # that were never supposed to exist -- a false alarm loud enough to
        # hide a real one.
        self.required_tables = list(
            required_tables if required_tables is not None else REQUIRED_TABLES)
        self.client = None
        self._initialized = False
        self.missing_tables: List[str] = []

    # ══════════════════════════════════════════════════════════
    #  CONNECTION
    # ══════════════════════════════════════════════════════════

    async def initialize(self):
        """Connects to Supabase and verifies every required table exists."""
        if not self.url or not self.key:
            logger.warning("Supabase URL/key not configured. Running in offline mode.")
            self._initialized = False
            return

        try:
            from supabase import create_client
            self.client = await asyncio.to_thread(create_client, self.url, self.key)
            self._initialized = True
            logger.info("Supabase connection established successfully.")
        except ImportError:
            logger.warning("Supabase SDK not installed. Offline mode. (pip install supabase)")
            self._initialized = False
            return
        except Exception as e:
            logger.error(f"Failed to connect to Supabase: {e}")
            self._initialized = False
            return

        await self.verify_schema()

    async def verify_schema(self) -> List[str]:
        """
        Checks every required table is reachable.

        A missing table previously caused silent write failures forever —
        this reports the problem once, clearly, with the fix.
        """
        if not self._initialized:
            return list(self.required_tables)

        missing = []
        for table in self.required_tables:
            try:
                await asyncio.to_thread(
                    lambda t=table: self.client.table(t).select("*").limit(1).execute()
                )
            except Exception as e:
                if "PGRST205" in str(e) or "could not find the table" in str(e).lower():
                    missing.append(table)
                else:
                    logger.warning(f"Table '{table}' check inconclusive: {str(e)[:100]}")

        self.missing_tables = missing
        if missing:
            logger.error(
                "=" * 68 + "\n"
                f"  MISSING SUPABASE TABLES: {', '.join(missing)}\n"
                "  Data written to these tables is being DISCARDED.\n"
                "  Fix: open the Supabase SQL Editor and run database/schema.sql\n"
                + "=" * 68
            )
        else:
            logger.info(f"Supabase schema verified — all {len(REQUIRED_TABLES)} tables present.")
        return missing

    async def _run(self, fn, label: str, default=None):
        """Runs a blocking Supabase call in a thread; never raises."""
        if not self._initialized:
            return default
        try:
            return await asyncio.to_thread(fn)
        except Exception as e:
            logger.error(f"Supabase {label} failed: {str(e)[:200]}")
            return default

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    # ══════════════════════════════════════════════════════════
    #  POSTS
    # ══════════════════════════════════════════════════════════

    async def log_post(self, platform: str, content: str, image_path: str = "",
                       status: str = "posted", metadata: Dict = None) -> Optional[str]:
        """Logs a published post."""
        if not self._initialized:
            logger.warning(f"[OFFLINE] Would log {platform} post: {content[:50]}...")
            return None

        data = {
            "platform": platform,
            "content": (content or "")[:4000],
            "image_path": image_path or "",
            "status": status,
            "metadata": metadata or {},
            "created_at": self._now(),
        }
        result = await self._run(
            lambda: self.client.table("posts").insert(data).execute(), "log_post")
        if result and result.data:
            logger.info(f"Logged {platform} post to Supabase (status: {status}).")
            return result.data[0].get("id")
        return None

    async def get_today_post_count(self, platform: str) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = await self._run(
            lambda: (self.client.table("posts").select("id", count="exact")
                     .eq("platform", platform).eq("status", "posted")
                     .gte("created_at", f"{today}T00:00:00Z").execute()),
            "get_today_post_count")
        return (result.count or 0) if result else 0

    # ══════════════════════════════════════════════════════════
    #  ALERTS / METRICS / ERRORS
    # ══════════════════════════════════════════════════════════

    async def log_alert(self, level: str, module: str, message: str):
        if not self._initialized:
            logger.warning(f"[OFFLINE ALERT] [{level}] {module}: {message}")
            return
        data = {"level": level, "module": module, "message": (message or "")[:2000],
                "resolved": False, "created_at": self._now()}
        await self._run(lambda: self.client.table("alerts").insert(data).execute(), "log_alert")

    async def log_metric(self, metric_name: str, value: float, metadata: Dict = None):
        if not self._initialized:
            return
        data = {"metric_name": metric_name, "value": float(value),
                "metadata": metadata or {}, "created_at": self._now()}
        await self._run(lambda: self.client.table("metrics").insert(data).execute(), "log_metric")

    async def log_error(self, module: str, error_type: str,
                        error_message: str, auto_resolved: bool = False):
        if not self._initialized:
            logger.warning(f"[OFFLINE ERROR] {module}: {error_message}")
            return
        data = {"module": module, "error_type": error_type,
                "error_message": (error_message or "")[:2000],
                "auto_resolved": auto_resolved, "created_at": self._now()}
        await self._run(lambda: self.client.table("error_logs").insert(data).execute(), "log_error")

        if not auto_resolved:
            await self.log_alert("WARNING", module, f"Unresolved error: {error_message[:200]}")

    async def get_active_alerts(self) -> List[Dict]:
        result = await self._run(
            lambda: (self.client.table("alerts").select("*").eq("resolved", False)
                     .order("created_at", desc=True).limit(50).execute()),
            "get_active_alerts")
        return (result.data or []) if result else []

    # ══════════════════════════════════════════════════════════
    #  BOT STATE — survives restarts and redeploys
    # ══════════════════════════════════════════════════════════

    async def save_state(self, key: str, value: Any):
        """Persists a piece of state (module toggles, limits, counters)."""
        data = {"key": key, "value": value, "updated_at": self._now()}
        await self._run(
            lambda: self.client.table("bot_state").upsert(data, on_conflict="key").execute(),
            f"save_state[{key}]")

    async def load_state(self, key: str, default=None):
        result = await self._run(
            lambda: self.client.table("bot_state").select("value").eq("key", key).limit(1).execute(),
            f"load_state[{key}]")
        if result and result.data:
            return result.data[0].get("value", default)
        return default

    async def load_all_state(self) -> Dict[str, Any]:
        result = await self._run(
            lambda: self.client.table("bot_state").select("key,value").execute(),
            "load_all_state")
        if result and result.data:
            return {row["key"]: row["value"] for row in result.data}
        return {}

    # ══════════════════════════════════════════════════════════
    #  MEDIA (Supabase Storage)
    # ══════════════════════════════════════════════════════════

    async def upload_image(self, local_path: str, dest_name: str = "",
                           bucket: str = "") -> str:
        """
        Publishes a locally generated image and returns its public URL.

        The bot's own disk is ephemeral — Render wipes it on every deploy — so
        an article hero has to live somewhere durable and publicly reachable
        before the website can render it. Returns "" on failure; callers treat
        that as "no hero image" rather than a hard error.

        `bucket` lets a second agent keep its images separate — the pin agent
        writes to its own bucket rather than mixing product pictures in with
        news article heroes.
        """
        if not self._initialized or not local_path or not os.path.exists(local_path):
            return ""

        name = dest_name or os.path.basename(local_path)
        try:
            with open(local_path, "rb") as fh:
                blob = fh.read()
        except OSError as e:
            logger.error(f"Could not read image for upload: {e}")
            return ""

        def _put():
            storage = self.client.storage.from_(bucket or IMAGE_BUCKET)
            storage.upload(
                path=name,
                file=blob,
                file_options={"content-type": "image/jpeg",
                              "cache-control": "31536000",
                              "upsert": "true"},
            )
            return storage.get_public_url(name)

        # Retried, because a single transient failure costs a whole slot.
        # The first live pin's second attempt died on an HTTP 520 from
        # Supabase storage -- a Cloudflare hiccup, gone seconds later --
        # and the pin was dropped with "image not hosted". Storage sits
        # behind a CDN, so 5xx here means "try again", not "give up".
        url = ""
        for attempt in range(1, 4):
            url = await self._run(_put, "upload_image", default="")
            if url:
                break
            if attempt < 3:
                logger.warning(f"Image upload attempt {attempt} failed; "
                               f"retrying in {attempt * 2}s.")
                await asyncio.sleep(attempt * 2)

        if url:
            url = url.rstrip("?")          # supabase-py appends a bare '?' on some versions
            logger.info(f"Article image uploaded: {url}")
            return url

        logger.error(
            f"Image upload failed. Does the public '{bucket or IMAGE_BUCKET}' storage "
            f"bucket exist? Run the matching schema file, or create it in "
            f"Supabase → Storage."
        )
        return ""

    # ══════════════════════════════════════════════════════════
    #  ARTICLES (website / auto-blogging)
    # ══════════════════════════════════════════════════════════

    async def save_article(self, article: Dict) -> Optional[Dict]:
        """Inserts an article, or updates it if the slug already exists."""
        if not self._initialized:
            logger.warning(f"[OFFLINE] Would save article: {article.get('slug')}")
            return None

        payload = dict(article)
        payload.setdefault("published_at", self._now())
        payload.setdefault("created_at", self._now())

        result = await self._run(
            lambda: self.client.table("articles").upsert(payload, on_conflict="slug").execute(),
            "save_article")
        if result and result.data:
            logger.info(f"Article saved to website: /{payload.get('slug')}")
            return result.data[0]
        return None

    async def slug_exists(self, slug: str) -> bool:
        result = await self._run(
            lambda: self.client.table("articles").select("slug").eq("slug", slug).limit(1).execute(),
            "slug_exists")
        return bool(result and result.data)

    async def get_articles(self, limit: int = 20, category: str = "") -> List[Dict]:
        def q():
            builder = (self.client.table("articles").select("*")
                       .eq("status", "published").order("published_at", desc=True).limit(limit))
            if category:
                builder = builder.eq("category", category)
            return builder.execute()
        result = await self._run(q, "get_articles")
        return (result.data or []) if result else []

    async def recent_articles(self, limit: int = 80) -> List[Dict]:
        """
        Just enough of each published article to decide whether to link it.

        Deliberately not get_articles(): that selects *, and pulling eighty
        full article bodies to choose four internal links moves megabytes to
        read four slugs.
        """
        def q():
            return (self.client.table("articles")
                    .select("slug,title,category,seo_keywords,published_at")
                    .eq("status", "published")
                    .order("published_at", desc=True).limit(limit).execute())
        result = await self._run(q, "recent_articles")
        return (result.data or []) if result else []

    async def article_bodies(self, limit: int = 400) -> List[Dict]:
        """
        Article bodies, for counting how many internal links point where.

        Deliberately separate from recent_articles(), which is kept lean so
        that choosing four links does not move megabytes. This one DOES move
        the bodies, so the caller reads it a few times a day rather than
        once per article.
        """
        def q():
            return (self.client.table("articles").select("slug,content")
                    .eq("status", "published")
                    .order("published_at", desc=True).limit(limit).execute())
        result = await self._run(q, "article_bodies")
        return (result.data or []) if result else []

    async def get_article_by_slug(self, slug: str) -> Optional[Dict]:
        result = await self._run(
            lambda: self.client.table("articles").select("*").eq("slug", slug).limit(1).execute(),
            "get_article_by_slug")
        if result and result.data:
            return result.data[0]
        return None

    # ══════════════════════════════════════════════════════════
    #  SCRAPED USERS (stealth dedupe)
    # ══════════════════════════════════════════════════════════

    async def upsert_scraped_users(self, rows: List[Dict]) -> bool:
        if not rows:
            return True
        result = await self._run(
            lambda: self.client.table("scraped_users").upsert(rows, on_conflict="user_id").execute(),
            "upsert_scraped_users")
        return result is not None

    async def get_invited_user_ids(self) -> set:
        result = await self._run(
            lambda: self.client.table("scraped_users").select("user_id").eq("invited", True).execute(),
            "get_invited_user_ids")
        if result and result.data:
            return {row["user_id"] for row in result.data}
        return set()

    async def mark_user_invited(self, user_id: int, outcome: str = "sent"):
        await self._run(
            lambda: (self.client.table("scraped_users")
                     .update({"invited": True, "invited_at": self._now(), "outcome": outcome})
                     .eq("user_id", user_id).execute()),
            "mark_user_invited")

    # ══════════════════════════════════════════════════════════
    #  SOCIAL POSTS (cross-platform delivery tracking)
    # ══════════════════════════════════════════════════════════

    async def log_social_post(self, platform: str, content: str, status: str,
                              provider: str = "direct", image_url: str = "",
                              external_id: str = "", error: str = "",
                              article_slug: str = "") -> Optional[str]:
        data = {
            "platform": platform, "provider": provider,
            "content": (content or "")[:4000], "image_url": image_url,
            "status": status, "external_id": external_id,
            "error": (error or "")[:1000], "article_slug": article_slug,
            "created_at": self._now(),
            "sent_at": self._now() if status == "sent" else None,
        }
        result = await self._run(
            lambda: self.client.table("social_posts").insert(data).execute(), "log_social_post")
        if result and result.data:
            return result.data[0].get("id")
        return None
