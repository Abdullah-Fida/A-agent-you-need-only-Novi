"""
The 168 scheduled pictures, made once and kept in Supabase.

WHY THIS EXISTS. Making the picture inside the publishing slot puts a
consumer Gemini cookie on the critical path of every pin. That cookie is
the least reliable thing in the system: it expires on its own, it dies the
moment the account signs out anywhere, and when it dies it does not fail --
it answers in friendly English with no image attached, and the slot falls
back to a stock photograph. A day of that went out before anybody noticed.

Separating the two fixes it properly. The pictures are made in a batch,
whenever a cookie happens to be alive, and uploaded here. Publishing then
reads a URL out of Supabase, which is the same thing it already does for
every pin image it has ever posted. A dead cookie stops the NEXT batch and
costs nothing at publishing time.

That also makes the cookie's short life survivable. It does not have to
last a month; it has to last long enough to make some pictures. Whatever a
session manages before it dies is kept, and the next one carries on from
there -- see fill(), which is resumable by design.

THE KEY IS THE SCHEDULE SLOT, not the title. Day and slot already identify
a pin uniquely and never change, so a reworded title in the text file does
not orphan its picture. Re-run the batch with --redo to replace one.

WHAT IS STORED. One image per slot, at the key library/dDDsS.png, in the
same bucket the pin images already use. 168 of them, well inside the free
tier. Nothing else -- there is no index file to drift out of step with the
bucket, because the bucket IS the index and listing it is one call.
"""
import logging
import os
import time
from typing import Dict, List, Optional, Set

logger = logging.getLogger("PinAgent.PictureLibrary")

PREFIX = "library"

# The batch writes PNG because that is what the web app hands back, and
# converting would cost quality for no gain -- Pinterest accepts both.
EXT = "png"


def key_for(day: int, slot: int) -> str:
    """library/d01s1.png -- fixed width so a listing sorts in order."""
    return "%s/d%02ds%d.%s" % (PREFIX, int(day), int(slot), EXT)


class PictureLibrary:
    """Reads and writes the made pictures. Safe to construct with no client."""

    def __init__(self, client=None, bucket: str = ""):
        self.client = client
        self.bucket = bucket or os.getenv("PIN_BUCKET") or "pin-images"
        self._keys: Optional[Set[str]] = None
        self._listed_at = 0.0
        self.last_error = ""

    @property
    def is_ready(self) -> bool:
        return self.client is not None

    # ── what is in there ─────────────────────────────────────────

    def refresh(self) -> Set[str]:
        """
        The keys actually present, read from the bucket itself.

        Listed rather than remembered. An index file would be one more
        thing to keep in step, and the failure it invites is the worst
        kind: an index that claims a picture the bucket does not hold
        sends a pin out with a broken image URL.
        """
        if not self.is_ready:
            self._keys = set()
            return self._keys
        try:
            found: Set[str] = set()
            store = self.client.storage.from_(self.bucket)
            # Supabase pages listings; 100 is the default and 168 is more
            # than that, so ask explicitly rather than quietly losing the
            # tail of the library.
            offset = 0
            while True:
                page = store.list(PREFIX, {"limit": 200, "offset": offset})
                if not page:
                    break
                for row in page:
                    name = row.get("name") if isinstance(row, dict) else None
                    if name and name.endswith("." + EXT):
                        found.add("%s/%s" % (PREFIX, name))
                if len(page) < 200:
                    break
                offset += len(page)
            self._keys = found
        except Exception as e:
            self.last_error = "%s: %s" % (type(e).__name__, e)
            logger.warning("Could not list the picture library: %s"
                           % self.last_error)
            self._keys = set()
        self._listed_at = time.monotonic()
        return self._keys

    def refresh_if_stale(self, seconds: float = 600.0) -> Set[str]:
        """
        Re-list, but not more than once every few minutes.

        The batch fills the bucket while the bot is running, so a lookup
        that misses may be asking a minute too early. Re-listing on a miss
        picks those up without a restart; rate-limiting it stops a day
        whose pictures genuinely do not exist from listing the bucket once
        per slot forever.
        """
        if self._keys is None or time.monotonic() - self._listed_at > seconds:
            return self.refresh()
        return self._keys or set()

    @property
    def keys(self) -> Set[str]:
        if self._keys is None:
            self.refresh()
        return self._keys or set()

    def has(self, day: int, slot: int) -> bool:
        return key_for(day, slot) in self.keys

    def url_for(self, day: int, slot: int) -> str:
        """
        The public URL for one slot's picture, or "" if it was never made.

        No cache-buster, deliberately. These never change once written --
        replacing one goes through --redo, which changes the bytes under
        the same key and is rare enough to wait out a CDN. The pin images
        beside them are served the same way.
        """
        if not self.has(day, slot):
            return ""
        try:
            url = self.client.storage.from_(self.bucket).get_public_url(
                key_for(day, slot))
            return (url or "").rstrip("?")
        except Exception as e:
            self.last_error = "%s: %s" % (type(e).__name__, e)
            return ""

    # ── putting one in ───────────────────────────────────────────

    # REPLACING, NOT DELETING. Measured against the live bucket: remove()
    # returns an empty list and the object stays, because the key this runs
    # under may upload but not delete. Every write therefore goes through
    # upsert, which was checked the same way -- a second upload under the
    # same key changed the bytes served.
    #
    # That is enough for everything this needs. A picture is replaced in
    # place by --redo, and a slot whose picture should go away does not
    # arise: the schedule always wants exactly 168. Clearing the library
    # for real needs the service_role key from the Supabase dashboard.

    def put(self, day: int, slot: int, local_path: str) -> str:
        """
        Uploads one picture and returns its public URL, or "".

        Overwrites whatever was at that slot. See the note above: upsert is
        the only way this key can replace a picture.
        """
        if not (self.is_ready and local_path and os.path.exists(local_path)):
            return ""
        key = key_for(day, slot)
        try:
            with open(local_path, "rb") as fh:
                blob = fh.read()
        except OSError as e:
            self.last_error = str(e)
            return ""
        if not blob:
            self.last_error = "the file was empty"
            return ""
        try:
            store = self.client.storage.from_(self.bucket)
            store.upload(path=key, file=blob,
                         file_options={"content-type": "image/%s" % EXT,
                                       "cache-control": "31536000",
                                       "upsert": "true"})
            if self._keys is not None:
                self._keys.add(key)
            return (store.get_public_url(key) or "").rstrip("?")
        except Exception as e:
            self.last_error = "%s: %s" % (type(e).__name__, e)
            logger.warning("Could not store d%02ds%d: %s"
                           % (day, slot, self.last_error))
            return ""

    # ── how full it is ───────────────────────────────────────────

    def missing(self, book) -> List[Dict]:
        """
        Every scheduled entry with no picture yet, in schedule order.

        This is what makes the batch resumable: it is derived from the
        bucket on every run, so a session that dies after nine pictures
        simply leaves 159 here for the next one.
        """
        out = []
        for day in sorted(book.days):
            for entry in book.days[day]:
                if not self.has(day, entry["slot"]):
                    out.append(entry)
        return out

    def status(self, book=None) -> Dict:
        held = len(self.keys)
        out = {"ready": self.is_ready, "bucket": self.bucket,
               "pictures": held, "last_error": self.last_error}
        if book is not None and book.is_ready:
            want = sum(len(v) for v in book.days.values())
            out["wanted"] = want
            out["missing"] = max(0, want - held)
            out["complete"] = held >= want
            out["today_ready"] = sum(
                1 for e in book.for_day() if self.has(e["day"], e["slot"]))
        return out
