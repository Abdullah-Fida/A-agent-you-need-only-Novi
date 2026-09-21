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
import json
import os
import httpx
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("PinAgent.Store")

# The `angle` column doubles as the pin's kind. Product pins carry one of the
# copywriter's angles; advice pins carry this. A separate column would need a
# migration against a live table for a distinction one existing column already
# makes, and every row in pin_posts was written by save_pin() below, which
# coerces angle to a string -- so it is never NULL and .eq/.neq are safe.
TIP_ANGLE = "tip"


class PinStore:
    """Reads and writes the pin agent's own tables."""

    def __init__(self, client=None):
        self.client = client
        self.enabled = client is not None
        if not self.enabled:
            logger.warning("No Supabase client — pin history is in memory only, "
                           "so duplicates will reappear after a restart.")
        self._memory: List[Dict] = []
        # Said once rather than on every pin. See save_pin.
        self._warned_missing_columns = False
        # Where the photo memory lives. See photo_memory().
        self.bucket = os.getenv("PIN_BUCKET") or "pin-images"
        self._photo_memory: Optional[Dict] = None

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

    @staticmethod
    def _is_kind(row: Dict, kind: str) -> bool:
        """Whether a stored pin is a product pin, an advice pin, or either."""
        if kind == "all":
            return True
        is_tip = (row.get("angle") or "") == TIP_ANGLE
        return is_tip if kind == "tip" else not is_tip

    async def posted_today(self, kind: str = "all") -> int:
        """
        How many pins have gone out today, for the daily cap.

        `kind` is what keeps the affiliate ratio honest: the cap counts every
        pin, while the product quota counts only the ones carrying a link.
        """
        if not self.enabled:
            today = datetime.now(timezone.utc).date().isoformat()
            return sum(1 for p in self._memory
                       if str(p.get("created_at", "")).startswith(today)
                       and p.get("status") == "published"
                       and self._is_kind(p, kind))

        start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).isoformat()

        def query():
            q = (self.client.table("pin_posts").select("id", count="exact")
                 .eq("status", "published").gte("created_at", start))
            if kind == "tip":
                q = q.eq("angle", TIP_ANGLE)
            elif kind == "product":
                q = q.neq("angle", TIP_ANGLE)
            return q.execute()

        result = await self._run(query)
        return getattr(result, "count", None) or 0

    # ── writing ──────────────────────────────────────────────────

    async def published_since(self, minutes: int) -> int:
        """
        How many pins published in the last `minutes`.

        Guards the slot against a restart: the loop's in-memory record of
        which slots have fired is wiped on every deploy, and a restart
        inside the window would publish the slot a second time.
        """
        if not self.enabled:
            return 0

        since = (datetime.now(timezone.utc)
                 - timedelta(minutes=minutes)).isoformat()

        def query():
            return (self.client.table("pin_posts").select("id", count="exact")
                    .eq("status", "published").gte("created_at", since).execute())

        result = await self._run(query)
        return getattr(result, "count", None) or 0

    async def recent_titles(self, limit: int = 40,
                            kind: str = "product") -> List[str]:
        """
        Titles of the most recent pins, whatever their status.

        Status is deliberately ignored: a pin waiting for review or already
        published both mean the product has been covered, and a near
        duplicate of either is what makes a board look automated.

        PRODUCT PINS ONLY, unless asked otherwise. Four pins in five are now
        advice pins from the tip bank, and their titles are hand-written
        sentences about tidying that share the whole generic vocabulary of
        the niche. Left in, they would fill this window with text no product
        can meaningfully be compared against -- the duplicate guard would be
        measuring against eight real products instead of forty, and the
        duplicates it exists to stop would come straight back.
        """
        def whole(row) -> str:
            # Title AND description. A pin title is five or six words, and
            # comparing two of them missed a duplicate that shared
            # "scissors, stainless, steel" in the body text.
            return f"{row.get('title') or ''} {row.get('description') or ''}".strip()

        if not self.enabled:
            rows = [p for p in self._memory if self._is_kind(p, kind)]
            return [whole(p) for p in rows[-limit:]]

        def query():
            q = (self.client.table("pin_posts").select("title,description,angle")
                 .order("created_at", desc=True).limit(limit))
            if kind == "tip":
                q = q.eq("angle", TIP_ANGLE)
            elif kind == "product":
                q = q.neq("angle", TIP_ANGLE)
            return q.execute()

        result = await self._run(query)
        return [whole(r) for r in (getattr(result, "data", None) or [])]

    # ── photographs remembered in the image bucket ───────────────
    #
    # NOT IN THE DATABASE, deliberately. The photo columns need a migration
    # run by hand, and until that happens every photograph the bot has used
    # is forgotten on restart -- which on Render is every deploy and every
    # wake from sleep, and is how one egg timer published twice in two
    # hours.
    #
    # The bucket needs nothing run. It already holds every finished pin, so
    # one small JSON file beside them is durable today.
    PHOTO_MEMORY = "memory/photos.json"
    USED_LIMIT = 400
    POOL_LIMIT = 400

    async def photo_memory(self) -> Dict[str, List]:
        """
        {"used": [url, ...], "pool": [{"photo_url", "photo_note"}, ...]}

        `used` is every photograph published, newest first -- the duplicate
        guard. `pool` is every photograph the vision check approved, with
        the description it gave, so one can be written about again without
        a search or a second check.
        """
        empty = {"used": [], "pool": []}
        if not self.enabled:
            return dict(self._photo_memory or empty)

        def read():
            store = self.client.storage.from_(self.bucket)
            # THROUGH THE PUBLIC URL, WITH A CACHE-BUSTER. Storage is behind
            # a CDN, which is right for the pin images -- they never change
            # -- and wrong for this file, which changes after every pin.
            # Measured live: storage listed 851 bytes while download() kept
            # handing back a 24-byte copy from before the write. A stale
            # read here silently re-allows a photograph already published.
            try:
                url = store.get_public_url(self.PHOTO_MEMORY).rstrip("?")
                sep = "&" if "?" in url else "?"
                r = httpx.get(f"{url}{sep}v={int(time.time())}",
                              timeout=30,
                              headers={"Cache-Control": "no-cache"})
                if r.status_code == 200:
                    return r.content
                if r.status_code != 404:
                    logger.info(f"Photo memory read returned HTTP "
                                f"{r.status_code}; trying storage directly.")
            except Exception as e:
                logger.info(f"Photo memory read failed ({type(e).__name__}); "
                            f"trying storage directly.")

            # A private bucket has no public URL, so fall back.
            try:
                return store.download(self.PHOTO_MEMORY)
            except Exception as e:
                # THE FIRST RUN HAS NO MEMORY YET, and that is normal rather
                # than a fault. Logging it as an error taught everyone to
                # ignore the one line that matters when it really breaks.
                if "not_found" in str(e) or "404" in str(e):
                    logger.info("No photo memory yet; starting one.")
                    return None
                raise

        raw = await self._run(read)
        if not raw:
            return empty
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            logger.warning(f"The photo memory could not be read ({e}); "
                           f"starting a fresh one.")
            return empty
        used = [u for u in data.get("used", []) if isinstance(u, str) and u]
        pool = [p for p in data.get("pool", [])
                if isinstance(p, dict) and p.get("photo_url")
                and p.get("photo_note")]
        return {"used": used, "pool": pool}

    async def remember_photo(self, url: str, note: str = "") -> None:
        """
        Records a photograph as published, and keeps its description.

        Written after the pin is safely away, so a storage hiccup can cost
        the memory but never the pin.
        """
        url = (url or "").strip()
        if not url:
            return

        memory = await self.photo_memory()
        memory["used"] = [url] + [u for u in memory["used"] if u != url]
        del memory["used"][self.USED_LIMIT:]

        note = (note or "").strip()
        # A MADE picture is a local file, not a URL. It is remembered as
        # used -- it must never publish twice -- but it does not belong in
        # the reusable pool: the path is gone after the next deploy, and a
        # slot that picked it would spend a search failing to open it.
        if note and not url.lower().startswith("http"):
            note = ""
        if note:
            memory["pool"] = ([{"photo_url": url, "photo_note": note}]
                              + [p for p in memory["pool"]
                                 if p["photo_url"] != url])
            del memory["pool"][self.POOL_LIMIT:]

        if not self.enabled:
            self._photo_memory = memory
            return

        blob = json.dumps(memory, ensure_ascii=False).encode("utf-8")

        def write():
            self.client.storage.from_(self.bucket).upload(
                path=self.PHOTO_MEMORY, file=blob,
                file_options={"content-type": "application/json",
                              # Asks the CDN not to hold it. The read does
                              # not rely on this being honoured.
                              "cache-control": "no-store, max-age=0",
                              "upsert": "true"})
            return True

        if not await self._run(write):
            logger.warning("The photo memory could not be saved; a restart "
                           "may allow a photograph to repeat.")

    async def verified_photos(self, limit: int = 120) -> List[Dict[str, str]]:
        """
        Photographs the vision check has already approved, newest first.

        WHY THIS IS WORTH A QUERY. The free vision allowance is twenty
        requests per key per model per day. Every photograph that passed was
        being forgotten the moment the process restarted, so that allowance
        bought one pin each and nothing accumulated. Read back, the same
        calls build a pool that only grows.

        Reusing a photograph under a different tip is not a repeat pin --
        only the finished composite has to be unique, and the image hash
        already enforces that. What must not repeat is the pairing, and the
        caller holds recent_photos for that.
        """
        def usable(rows):
            # A URL without the description is no use here: the cached
            # tier exists to skip the vision call, and write_for_photo
            # takes the DESCRIPTION as its input, not the picture.
            out, seen = [], set()
            for row in rows:
                url = (row.get("photo_url") or "").strip()
                note = (row.get("photo_note") or "").strip()
                if not url or not note or url in seen:
                    continue
                seen.add(url)
                out.append({"photo_url": url, "photo_note": note})
            return out

        if not self.enabled:
            # Newest first, to match the ordering of the live query --
            # the caller reads the head of this list.
            return usable(reversed(self._memory))[:limit]

        def query():
            return (self.client.table("pin_posts")
                    .select("photo_url,photo_note")
                    .eq("angle", TIP_ANGLE)
                    .neq("photo_url", "")
                    .order("created_at", desc=True).limit(limit).execute())

        # _run swallows the error and returns None if the column is not
        # there yet, which reads back as an empty pool -- the live search
        # simply carries on as it did before.
        result = await self._run(query)
        return usable(getattr(result, "data", None) or [])

    async def last_affiliate_pin_at(self) -> Optional[datetime]:
        """
        When a pin carrying a link last published, or None.

        The daily counter cannot answer this. A product slot that fails
        falls through to an advice pin, so the slot SUCCEEDS and nothing
        counts as missed -- the affiliate pin stopped on 13 September and
        nobody knew for two days. Age is also the right question rather
        than "did one publish today": posted_today() is anchored to UTC
        midnight while the schedule runs on Pakistan days, and PKT midnight
        is 19:00 UTC the day before.
        """
        if not self.enabled:
            stamps = [p.get("created_at") for p in self._memory
                      if p.get("status") == "published"
                      and not self._is_kind(p, "tip")]
            if not stamps:
                return None
            newest = max(str(s) for s in stamps if s)
            try:
                parsed = datetime.fromisoformat(newest.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(
                    tzinfo=timezone.utc)
            except (ValueError, TypeError):
                return None

        def query():
            return (self.client.table("pin_posts").select("created_at")
                    .eq("status", "published").neq("angle", TIP_ANGLE)
                    .order("created_at", desc=True).limit(1).execute())

        result = await self._run(query)
        rows = getattr(result, "data", None) or []
        if not rows:
            return None
        try:
            stamp = str(rows[0].get("created_at") or "").replace("Z", "+00:00")
            parsed = datetime.fromisoformat(stamp)
            return parsed if parsed.tzinfo else parsed.replace(
                tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None

    async def recent_types(self, days: int = 7) -> List[str]:
        """
        Product types pinned inside the cooldown window.

        Read from the `category` column, which used to hold the AliExpress
        category and was therefore the word "Home & Garden" on all 47 pins --
        no signal at all, and nothing read it. It now holds the product type,
        which makes this query possible without a migration against a live
        table and makes category_performance() finally mean something.
        """
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        if not self.enabled:
            return [p.get("category") or "" for p in self._memory
                    if str(p.get("created_at", "")) >= since
                    and self._is_kind(p, "product")]

        def query():
            return (self.client.table("pin_posts").select("category")
                    .neq("angle", TIP_ANGLE)
                    .gte("created_at", since).limit(400).execute())

        result = await self._run(query)
        return [r.get("category") or ""
                for r in (getattr(result, "data", None) or []) if r.get("category")]

    async def recent_tip_titles(self, limit: int = 50) -> List[str]:
        """
        Titles of the advice pins published lately, so the bank rotates.

        Only the TITLE, because that is what the tip bank is keyed on -- the
        body text would never match.
        """
        if not self.enabled:
            return [p.get("title") or "" for p in self._memory
                    if self._is_kind(p, "tip")][-limit:]

        def query():
            return (self.client.table("pin_posts").select("title")
                    .eq("angle", TIP_ANGLE)
                    .order("created_at", desc=True).limit(limit).execute())

        result = await self._run(query)
        return [r.get("title") or "" for r in (getattr(result, "data", None) or [])]

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
            # The SOURCE photograph and what the vision check saw in it.
            # image_url above is the finished composite; these are what let
            # a verified picture be reused instead of re-verified.
            "photo_url": (pin.get("photo_url") or "")[:1000],
            "photo_note": (pin.get("photo_note") or "")[:400],
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

        # THE NEW COLUMNS MAY NOT BE THERE YET, and a pin is worth more than
        # a cached photograph. photo_url and photo_note are additive, so a
        # table that predates them rejects the whole INSERT -- which would
        # stop every pin being recorded, and everything that reads the table
        # with it: the duplicate guard, the daily cap, the ramp.
        #
        # So the write is retried without them. The bot keeps working the
        # moment this ships and gains the cache when the migration is run,
        # in either order.
        if any(k in record for k in ("photo_url", "photo_note")):
            trimmed = {k: v for k, v in record.items()
                       if k not in ("photo_url", "photo_note")}

            def insert_trimmed():
                return self.client.table("pin_posts").insert(trimmed).execute()

            result = await self._run(insert_trimmed)
            rows = getattr(result, "data", None) or []
            if rows:
                if not self._warned_missing_columns:
                    self._warned_missing_columns = True
                    logger.warning(
                        "pin_posts has no photo_url/photo_note column, so "
                        "verified photographs cannot be cached and every "
                        "vision check is spent again. Run "
                        "database/pin_schema.sql in the SQL editor -- it is "
                        "additive and safe to re-run.")
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
            # Advice pins excluded. They carry a BOARD name in `category`
            # rather than an AliExpress category, so they match nothing the
            # selector looks up -- but they would still land in the average
            # every category is measured against, and they are expected to
            # out-engage product pins by a wide margin. That would drag every
            # real category below 1.0 and quietly bias the ranking towards
            # categories with no history at all.
            return (self.client.table("pin_posts")
                    .select("category,saves,clicks")
                    .neq("angle", TIP_ANGLE)
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
