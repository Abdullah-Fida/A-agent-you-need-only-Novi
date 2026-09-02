"""
Persistence for the pin agent.

Runs against its OWN Supabase project when PIN_SUPABASE_URL is set, and
falls back to Novi's otherwise. Two projects rather than one for a practical
reason: the free tier gives 1 GB of file storage per project, and article
heroes at eight a day plus pin images at four a day fill a shared bucket in
about a year. Separate projects give each a full gigabyte, and a problem
with one cannot reach the other.

Every call is wrapped in asyncio.to_thread because supabase-py is
synchronous — awaiting it directly returns a coroutine that is silently
discarded, which is how scraped users and articles went missing on the news
side for weeks.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("PinAgent.Store")


class PinStore:
    """Reads and writes the pin agent's own tables."""

    def __init__(self, client=None):
        self.client = client
        self.enabled = client is not None
        if not self.enabled:
            logger.warning("No Supabase client — pin history is in memory only, "
                           "so duplicates will reappear after a restart.")
        self._memory: List[Dict] = []

    async def _run(self, fn, default=None):
        if not self.enabled:
            return default
        try:
            return await asyncio.to_thread(fn)
        except Exception as e:
            logger.error(f"Supabase call failed: {type(e).__name__}: {e}")
            return default

    # ── history / dedupe ─────────────────────────────────────────

    async def posted_history(self, days: int = 120) -> Dict[str, List[str]]:
        """
        Everything already pinned, for the dedupe sets.

        Bounded by age so the sets stay small; a product not pinned in four
        months is fair to feature again.
        """
        if not self.enabled:
            return {
                "product_ids": [p.get("product_id", "") for p in self._memory],
                "urls": [p.get("link", "") for p in self._memory],
                "image_hashes": [p.get("image_hash", "") for p in self._memory],
            }

        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        def query():
            return (self.client.table("pin_posts")
                    .select("product_id,link,image_hash")
                    .gte("created_at", since)
                    .limit(5000).execute())

        result = await self._run(query)
        rows = getattr(result, "data", None) or []
        return {
            "product_ids": [r.get("product_id", "") for r in rows],
            "urls": [r.get("link", "") for r in rows],
            "image_hashes": [r.get("image_hash", "") for r in rows],
        }

    async def recent_angles(self, limit: int = 6) -> List[str]:
        """The angles used most recently, so the copywriter can vary."""
        if not self.enabled:
            return [p.get("angle", "") for p in self._memory[-limit:]]

        def query():
            return (self.client.table("pin_posts").select("angle")
                    .order("created_at", desc=True).limit(limit).execute())

        result = await self._run(query)
        return [r.get("angle", "") for r in (getattr(result, "data", None) or [])]

    async def posted_today(self) -> int:
        """How many pins have gone out today, for the daily cap."""
        if not self.enabled:
            today = datetime.now(timezone.utc).date().isoformat()
            return sum(1 for p in self._memory
                       if str(p.get("created_at", "")).startswith(today))

        start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).isoformat()

        def query():
            return (self.client.table("pin_posts").select("id", count="exact")
                    .eq("status", "published").gte("created_at", start).execute())

        result = await self._run(query)
        return getattr(result, "count", None) or 0

    # ── writing ──────────────────────────────────────────────────

    async def first_pin_at(self) -> Optional[datetime]:
        """
        When the first pin was published, or None if none has been.

        Feeds the volume ramp. Derived from the pins rather than stored as
        its own setting, because a separate "started on" value is exactly
        what a redeploy wipes -- which silently put the social module back
        on its full daily cap on day one.
        """
        if not self.enabled:
            return None

        def query():
            return (self.client.table("pin_posts").select("created_at")
                    .eq("status", "published")
                    .order("created_at", desc=False).limit(1).execute())

        result = await self._run(query)
        rows = getattr(result, "data", None) or []
        if not rows:
            return None
        try:
            stamp = str(rows[0].get("created_at") or "").replace("Z", "+00:00")
            parsed = datetime.fromisoformat(stamp)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None

    async def pending_pins(self, days: int = 14) -> List[Dict]:
        """
        Pins still waiting for a human decision.

        The review queue lived only in memory, so every Render restart --
        which is every deploy -- emptied it. A pin was built, saved, emailed
        for approval, and then had nowhere to be approved: pressing Approve
        found an empty list. Worse, posted_history() reads every pin_posts
        row regardless of status, so the orphaned product entered the dedupe
        set and was blocked from being featured again for 120 days.

        Bounded by age because a pin nobody has judged in a fortnight is
        stale: its price has moved and the listing may be gone.
        """
        if not self.enabled:
            return [p for p in self._memory
                    if p.get("status") == "awaiting_review"]

        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        def query():
            return (self.client.table("pin_posts").select("*")
                    .eq("status", "awaiting_review")
                    .gte("created_at", since)
                    .order("created_at", desc=False).limit(200).execute())

        result = await self._run(query)
        return getattr(result, "data", None) or []

    async def save_pin(self, pin: Dict) -> Optional[Dict]:
        record = {
            "product_id": str(pin.get("product_id", ""))[:64],
            "title": (pin.get("title") or "")[:200],
            "description": (pin.get("description") or "")[:2000],
            "link": (pin.get("link") or "")[:1000],
            "image_url": (pin.get("image_url") or "")[:1000],
            "image_hash": (pin.get("image_hash") or "")[:64],
            "angle": (pin.get("angle") or "")[:40],
            "category": (pin.get("category") or "")[:80],
            "score": float(pin.get("score") or 0),
            "status": pin.get("status", "queued"),
            "external_id": (pin.get("external_id") or "")[:120],
        }

        if not self.enabled:
            record["created_at"] = datetime.now(timezone.utc).isoformat()
            self._memory.append(record)
            return record

        def insert():
            return self.client.table("pin_posts").insert(record).execute()

        result = await self._run(insert)
        rows = getattr(result, "data", None) or []
        if rows:
            logger.info(f"Pin recorded: {record['title'][:44]}")
            return rows[0]
        logger.error("Pin could not be saved. Has database/pin_schema.sql been run?")
        return None

    async def mark_status(self, product_id: str, status: str,
                          external_id: str = "") -> bool:
        if not self.enabled:
            for row in self._memory:
                if row.get("product_id") == str(product_id):
                    row["status"] = status
            return True

        patch = {"status": status}
        if external_id:
            patch["external_id"] = external_id

        def update():
            return (self.client.table("pin_posts").update(patch)
                    .eq("product_id", str(product_id)).execute())

        return bool(await self._run(update))

    # ── analytics feedback ───────────────────────────────────────

    async def category_performance(self, days: int = 30) -> Dict[str, float]:
        """
        Multipliers per category, learned from real pin performance.

        Uses a 30-day window because Pinterest is a search engine rather than
        a feed: pins accumulate saves over weeks, so a 24-hour read would be
        fitting noise. Categories with too little data are left at 1.0 rather
        than being guessed at.
        """
        if not self.enabled:
            return {}

        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        def query():
            return (self.client.table("pin_posts")
                    .select("category,saves,clicks")
                    .gte("created_at", since)
                    .eq("status", "published").limit(2000).execute())

        result = await self._run(query)
        rows = getattr(result, "data", None) or []
        if len(rows) < 12:
            return {}

        totals: Dict[str, List[int]] = {}
        for row in rows:
            category = (row.get("category") or "").lower()
            if not category:
                continue
            engagement = (row.get("saves") or 0) + (row.get("clicks") or 0)
            totals.setdefault(category, []).append(engagement)

        overall = [v for values in totals.values() for v in values]
        if not overall:
            return {}
        average = sum(overall) / len(overall)
        if average <= 0:
            return {}

        performance = {}
        for category, values in totals.items():
            if len(values) < 4:            # too thin to draw a conclusion
                continue
            ratio = (sum(values) / len(values)) / average
            # Clamped: one lucky pin should nudge the ranking, not rewrite it.
            performance[category] = round(min(max(ratio, 0.6), 1.6), 3)

        if performance:
            logger.info(f"Category performance learned: {performance}")
        return performance
