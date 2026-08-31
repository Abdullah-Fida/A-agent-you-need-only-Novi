"""
Real photographs for articles that have no wire photo.

A news story arrives with the picture its outlet published. An explainer
does not, and a generated illustration is visibly synthetic in a way that
undermines a news site -- readers can tell, and it is the single clearest
signal that nobody was involved.

So explainers get a real photograph from Openverse, which indexes
openly-licensed images across Flickr, Wikimedia and others. No API key, no
account.

Licensing is handled properly rather than ignored:

  * "nd" (no derivatives) is never used. The image pipeline crops to
    1280x720, which is a derivative work.
  * "nc" (non-commercial) is never used. A site carrying ads or affiliate
    links is commercial.
  * Public domain (cc0, pdm) is preferred because it carries no attribution
    obligation at all.
  * Where an attributed licence is used, the credit is returned so the
    caller can print it. An uncredited CC-BY image is a licence breach, not
    a small oversight.
"""
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("OmniBot.StockPhotos")

ENDPOINT = "https://api.openverse.org/v1/images/"
UA = {"User-Agent": "PressVane/1.0 (+https://pressvane.com) article illustration"}

# Tried in order. Public domain first: it needs no credit line, so the page
# stays clean and there is nothing to get wrong.
LICENCE_TIERS = ["cc0,pdm", "by,by-sa"]

# Licences that must never be used, whatever a search returns.
FORBIDDEN = ("nd", "nc")

MIN_WIDTH = 800
MIN_HEIGHT = 450


class StockPhotoFinder:
    """Finds an openly-licensed photograph for a topic."""

    TIMEOUT = 20.0

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.found = 0
        self.misses = 0
        self.last_error = ""

    # Words too common to prove a match on their own.
    _WEAK = {"the", "a", "an", "and", "of", "in", "on", "for", "with", "to",
             "photo", "image", "picture", "new", "old", "big", "small"}

    @classmethod
    def _relevant(cls, item: Dict, query: str) -> bool:
        """
        Whether the result is actually about the thing that was asked for.

        Openverse ranks loosely once the licence filter narrows the pool, and
        will happily answer "financial report documents" with a photograph of
        an Egyptian papyrus -- technically a document, uselessly wrong on the
        page. At least one meaningful query word has to appear in the title or
        the tags.
        """
        wanted = {w for w in query.lower().split() if w not in cls._WEAK and len(w) > 2}
        if not wanted:
            return True

        haystack = (item.get("title") or "").lower()
        for tag in item.get("tags") or []:
            haystack += " " + str(tag.get("name", "")).lower()
        return any(w in haystack for w in wanted)

    @staticmethod
    def _usable(item: Dict) -> bool:
        licence = (item.get("license") or "").lower()
        if any(part in licence.split("-") for part in FORBIDDEN):
            return False
        if not (item.get("url") or "").startswith("http"):
            return False
        # A thumbnail upscaled into a 1280x720 hero looks worse than no
        # picture at all.
        return (item.get("width") or 0) >= MIN_WIDTH and \
               (item.get("height") or 0) >= MIN_HEIGHT

    @staticmethod
    def credit_for(item: Dict) -> str:
        """
        The attribution line, or "" when the licence does not require one.

        Public domain needs no credit. Everything else does, and it has to
        name the creator, the licence and where it came from.
        """
        licence = (item.get("license") or "").lower()
        if licence in ("cc0", "pdm"):
            return ""
        creator = (item.get("creator") or "Unknown").strip()
        source = (item.get("source") or "Openverse").strip()
        return f"Photo: {creator} / {source} (CC {licence.upper()})"

    async def find(self, query: str) -> Tuple[Optional[str], str]:
        """
        Returns (image_url, credit). Credit is "" for public-domain images.

        Never raises: an article without a stock photo falls back to the
        generator, and a lookup failure must not stop a publish.
        """
        if not self.enabled or not query.strip():
            return None, ""

        import httpx
        for licences in LICENCE_TIERS:
            try:
                async with httpx.AsyncClient(timeout=self.TIMEOUT,
                                             follow_redirects=True) as client:
                    r = await client.get(ENDPOINT, headers=UA, params={
                        "q": query,
                        "license": licences,
                        "page_size": 12,
                        "mature": "false",
                        "size": "large",
                    })
                if r.status_code != 200:
                    self.last_error = f"HTTP {r.status_code}"
                    continue
                results = (r.json() or {}).get("results") or []
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                logger.warning(f"Openverse lookup failed: {self.last_error}")
                continue

            for item in results:
                if self._usable(item) and self._relevant(item, query):
                    self.found += 1
                    credit = self.credit_for(item)
                    logger.info(f"Stock photo for '{query[:36]}': "
                                f"{item.get('license')} "
                                f"{item.get('width')}x{item.get('height')}"
                                f"{' (credit required)' if credit else ''}")
                    return item["url"], credit

        self.misses += 1
        logger.info(f"No usable stock photo for '{query[:40]}'; the generator "
                    f"will draw one instead.")
        return None, ""

    @property
    def status(self) -> Dict:
        return {"found": self.found, "misses": self.misses,
                "last_error": self.last_error}
