"""
Real photographs for articles that have no wire photo.

A news story arrives with the picture its outlet published. An explainer
does not, and a generated illustration is visibly synthetic in a way that
undermines a news site -- readers can tell, and it is the single clearest
signal that nobody was involved.

So explainers get a real photograph from Openverse, which indexes
openly-licensed images across Flickr, Wikimedia and others. No API key, no
account.

TWO SOURCES, NOT ONE.
Openverse went down on 2 September 2026 -- its API root answered in a second
while every image search hung until the timeout, whatever the parameters.
Every picture on this system came from that one endpoint, so a Binance draft
went out bare and evergreen articles would have fallen back to a drawn
illustration. A single third-party outage should not be able to do that, so
Wikimedia Commons now backs it up: the same openly-licensed pool, a separate
API, still no key.

Licensing is handled properly rather than ignored:

  * "nd" (no derivatives) is never used. The image pipeline crops to
    1280x720, which is a derivative work.
  * "nc" (non-commercial) is never used. A site carrying ads or affiliate
    links is commercial.
  * Public domain (cc0, pdm, pd) is preferred because it carries no
    attribution obligation at all.
  * Where an attributed licence is used, the credit is returned so the
    caller can print it. An uncredited CC-BY image is a licence breach, not
    a small oversight.
"""
import html
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("OmniBot.StockPhotos")

ENDPOINT = "https://api.openverse.org/v1/images/"

# Wikimedia's own API host, commons.wikimedia.org, is blocked outright by
# some ISPs -- Pakistan's among them, which is where this is developed.
# en.wikipedia.org runs the same MediaWiki API against the same shared file
# repository, returns Commons files with `imagerepository: "shared"`, and is
# not blocked. Same pictures, a route that works from both here and Render.
WIKI_ENDPOINT = "https://en.wikipedia.org/w/api.php"

UA = {"User-Agent": "PressVane/1.0 (+https://pressvane.com) article illustration"}

# Tried in order. Public domain first: it needs no credit line, so the page
# stays clean and there is nothing to get wrong.
LICENCE_TIERS = ["cc0,pdm", "by,by-sa"]

# Licences that must never be used, whatever a search returns.
FORBIDDEN = ("nd", "nc")

# Licence codes that carry no attribution obligation. Openverse says "cc0"
# and "pdm"; Wikimedia says "pd" for the same thing.
NO_CREDIT_NEEDED = ("cc0", "pdm", "pd")

MIN_WIDTH = 800
MIN_HEIGHT = 450

# Wide enough to crop a 1280x720 hero or a 1200x675 Binance card from, small
# enough that no download is refused for its size.
WIKI_THUMB_WIDTH = 1600

# Pillow cannot open SVG, and a TIFF hero is a 40MB download for nothing.
RASTER_MIMES = ("image/jpeg", "image/png", "image/webp")

# Commons is an encyclopaedia's file store, not a stock library: it holds
# icons, logos, diagrams, maps and screenshots alongside its photographs,
# and the search ranks them equally. "data center" returned an icon and
# "computer monitor" returned a transparent PNG cut-out -- both of which
# look like a mistake once cropped into a hero or a Binance data band.
# Openverse needs none of this; its pool is photographs to begin with.
NOT_A_PHOTOGRAPH = (
    "icon", "logo", "diagram", "clipart", "clip art", "remix",
    "transparent", "screenshot", "coat of arms", "flag of", "map of",
    "seal of", "sticker", "infographic", "poster", "svg", "font awesome",
    "emblem", "symbol", "pictogram", "wordmark", "banner of",
)


class StockPhotoFinder:
    """Finds an openly-licensed photograph for a topic."""

    # Twelve seconds, not twenty. A ladder of three queries across two
    # licence tiers is six requests, so a hanging endpoint used to stall a
    # publish for eighty seconds before giving up.
    TIMEOUT = 12.0

    # After this many consecutive failures Openverse is left alone for a
    # while and the fallback is used directly. Without it every article
    # during an outage pays the full timeout ladder over again.
    FAILURES_BEFORE_REST = 3
    REST_SECONDS = 900.0

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.found = 0
        self.misses = 0
        self.last_error = ""
        self.from_fallback = 0
        self._ov_failures = 0
        self._ov_resting_until = 0.0

    # Words too common to prove a match on their own.
    _WEAK = {"the", "a", "an", "and", "of", "in", "on", "for", "with", "to",
             "photo", "image", "picture", "new", "old", "big", "small", "file"}

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

    @classmethod
    def credit_for(cls, item: Dict) -> str:
        """
        The attribution line, or "" when the licence does not require one.

        Public domain needs no credit. Everything else does, and it has to
        name the creator, the licence and where it came from.
        """
        licence = (item.get("license") or "").lower()
        if licence in NO_CREDIT_NEEDED:
            return ""
        creator = (item.get("creator") or "Unknown").strip()
        source = (item.get("source") or "Openverse").strip()
        # Openverse says "by-sa"; Wikimedia says "cc-by-sa-2.0". Only the
        # first needs the "CC " prefix adding, or the line reads "CC CC-BY".
        label = licence.upper()
        if not label.startswith("CC"):
            label = f"CC {label}"
        return f"Photo: {creator} / {source} ({label})"

    async def find(self, query: str, exclude=()) -> Tuple[Optional[str], str]:
        """
        Returns (image_url, credit). Credit is "" for public-domain images.

        `exclude` holds photo URLs the caller has already tried -- usually
        because the picture turned out to be one another article is using.
        Without it a retry asks the same question and gets the same top
        result, which is how two Tech articles ended up byte-identical.

        Never raises: an article without a stock photo falls back to the
        generator, and a lookup failure must not stop a publish.
        """
        if not self.enabled or not query.strip():
            return None, ""

        ladder = self._query_ladder(query)

        # A three-word query is precise and often finds nothing openly
        # licensed: "hardware wallet security" returns an empty pool while
        # "hardware wallet" and then "wallet" do not. Broaden a step at a
        # time rather than jumping straight to a generated illustration.
        if self._openverse_awake():
            for attempt in ladder:
                found, credit = await self._search(attempt, exclude)
                if found:
                    return found, credit
        else:
            logger.info("Openverse is resting after repeated failures; "
                        "going straight to Wikimedia.")

        # Openverse had nothing, or Openverse is down. Same pool of openly
        # licensed pictures, a different front door -- but a much blunter
        # search, so it gets its own shorter ladder (see _wiki_ladder).
        for attempt in self._wiki_ladder(query):
            found, credit = await self._search_wikimedia(attempt, exclude)
            if found:
                self.from_fallback += 1
                return found, credit

        self.misses += 1
        logger.info(f"No usable stock photo for '{query[:40]}' from either "
                    f"source; the generator will draw one instead.")
        return None, ""

    @staticmethod
    def _query_ladder(query: str) -> List[str]:
        """
        The query, then two decisively broader versions of it.

        Widening one word at a time and then truncating the list meant a
        four-word query never reached its single-word fallback: "laptop remote
        work desk" only ever tried "laptop remote work" and "laptop remote",
        both as empty as the original. The steps jump straight to two words
        and then one, which is where the openly-licensed pool actually is.
        """
        words = query.split()
        ladder = [query]
        for cut in (2, 1):
            candidate = " ".join(words[:cut])
            if candidate and candidate not in ladder:
                ladder.append(candidate)
        return ladder

    @staticmethod
    def _wiki_ladder(query: str) -> List[str]:
        """
        The Wikimedia ladder deliberately STOPS before the single word.

        Openverse can be broadened safely because it indexes stock libraries,
        where one word returns a thousand generic photographs. Commons is an
        encyclopaedia's file store, so one word returns whatever page text
        happens to contain it: broadening "source code" to "source" returned
        a photograph of Anse Source d'Argent, a beach in the Seychelles, and
        it passed every check because "source" is in the title.

        Two words is where Commons stops being a place-name lookup.
        """
        words = query.split()
        ladder = [query]
        pair = " ".join(words[:2])
        if pair and pair not in ladder:
            ladder.append(pair)
        return ladder

    # -- the circuit breaker -------------------------------------

    def _openverse_awake(self) -> bool:
        return time.monotonic() >= self._ov_resting_until

    def _openverse_failed(self) -> None:
        self._ov_failures += 1
        if self._ov_failures >= self.FAILURES_BEFORE_REST:
            self._ov_resting_until = time.monotonic() + self.REST_SECONDS
            logger.warning(f"Openverse failed {self._ov_failures} times in a "
                           f"row; resting it for "
                           f"{int(self.REST_SECONDS / 60)} minutes.")

    def _openverse_worked(self) -> None:
        self._ov_failures = 0
        self._ov_resting_until = 0.0

    # -- Openverse -----------------------------------------------

    async def _search(self, query: str, exclude=()) -> Tuple[Optional[str], str]:
        """One search pass across the licence tiers."""
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
                    self._openverse_failed()
                    continue
                results = (r.json() or {}).get("results") or []
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                logger.warning(f"Openverse lookup failed: {self.last_error}")
                self._openverse_failed()
                continue

            # It answered. Whether it held a usable picture is a separate
            # question from whether the service is healthy.
            self._openverse_worked()

            for item in results:
                if item.get("url") in exclude:
                    continue
                if self._usable(item) and self._relevant(item, query):
                    self.found += 1
                    credit = self.credit_for(item)
                    logger.info(f"Stock photo for '{query[:36]}': "
                                f"{item.get('license')} "
                                f"{item.get('width')}x{item.get('height')}"
                                f"{' (credit required)' if credit else ''}")
                    return item["url"], credit

        return None, ""

    # -- Wikimedia Commons, the fallback -------------------------

    @staticmethod
    def _strip_html(value: str) -> str:
        """The Artist field is a rendered HTML link, not a name."""
        return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value or "")).split())

    @classmethod
    def _from_wikimedia(cls, page: Dict) -> Optional[Dict]:
        """
        Reshapes a MediaWiki page into the same dict Openverse returns, so
        the licence, size and relevance checks are shared rather than
        written twice and drifting apart.
        """
        info = (page.get("imageinfo") or [None])[0]
        if not info:
            return None
        if info.get("mime") not in RASTER_MIMES:
            return None

        meta = info.get("extmetadata") or {}

        def field(name: str) -> str:
            return str((meta.get(name) or {}).get("value") or "").strip()

        # The scaled copy where one exists, the original otherwise (MediaWiki
        # returns no thumbnail when the file is already narrower than the
        # width asked for). Wikimedia appends utm_ tracking to both; dropping
        # it keeps the exclude-list keys stable, so a picture rejected on one
        # run is still recognised as the same picture on the next.
        url = (info.get("thumburl") or info.get("url") or "").split("?")[0]

        # "File:London Stock Exchange geograph-3066495.jpg" is the only
        # description a Commons file carries, so it is what relevance reads.
        title = page.get("title") or ""
        if title.lower().startswith("file:"):
            title = title[5:]
        title = title.rsplit(".", 1)[0].replace("_", " ")

        lowered = title.lower()
        if any(marker in lowered for marker in NOT_A_PHOTOGRAPH):
            return None

        return {"url": url,
                "width": info.get("width") or 0,
                "height": info.get("height") or 0,
                "license": field("License").lower(),
                "creator": cls._strip_html(field("Artist")) or "Unknown",
                "source": "Wikimedia Commons",
                "title": title,
                "tags": []}

    async def _search_wikimedia(self, query: str,
                                exclude=()) -> Tuple[Optional[str], str]:
        """One search pass against the File: namespace."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT,
                                         follow_redirects=True) as client:
                r = await client.get(WIKI_ENDPOINT, headers=UA, params={
                    "action": "query",
                    "format": "json",
                    "generator": "search",
                    "gsrsearch": query,
                    "gsrnamespace": 6,        # the File: namespace
                    "gsrlimit": 12,
                    "prop": "imageinfo",
                    "iiprop": "url|size|extmetadata|mime",
                    # Commons serves the ORIGINAL file, which is routinely a
                    # 3648x5419 camera frame well over the caller's 12MB
                    # download cap -- so good photographs were being found
                    # and then thrown away. Openverse hands back web-sized
                    # images, so this never came up there. Ask for a scaled
                    # copy and download that instead.
                    "iiurlwidth": WIKI_THUMB_WIDTH,
                })
            if r.status_code != 200:
                self.last_error = f"Wikimedia HTTP {r.status_code}"
                logger.warning(f"Wikimedia lookup failed: {self.last_error}")
                return None, ""
            pages = ((r.json() or {}).get("query") or {}).get("pages") or {}
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.warning(f"Wikimedia lookup failed: {self.last_error}")
            return None, ""

        # Commons has no tags and ranks on page text, so "one query word is
        # in the title" -- which is enough for Openverse -- lets a beach
        # through for "source code". Every candidate is scored on how much
        # of the query it actually matches, the best one wins, and a weak
        # best is refused outright. No picture beats the wrong picture.
        best = self._pick_wikimedia(pages, query, exclude)
        if not best:
            return None, ""

        self.found += 1
        credit = self.credit_for(best)
        logger.info(f"Wikimedia photo for '{query[:36]}' "
                    f"(matched {self._match_score(best, query)} words): "
                    f"{best.get('license')} "
                    f"{best.get('width')}x{best.get('height')}"
                    f"{' (credit required)' if credit else ''}")
        return best["url"], credit

    @classmethod
    def _pick_wikimedia(cls, pages: Dict, query: str,
                        exclude=()) -> Optional[Dict]:
        """
        The best candidate, or None when none is good enough.

        Ties break toward public domain. Openverse asks for cc0/pdm in its
        first pass and only widens to attributed licences afterwards; Commons
        has no such tiering, so without this the fallback returns an
        attribution-required photograph far more often than the primary
        source ever did, for no gain in quality.
        """
        best, best_key = None, None
        for page in (pages or {}).values():
            item = cls._from_wikimedia(page)
            if not item or item["url"] in exclude or not cls._usable(item):
                continue
            free = (item.get("license") or "").lower() in NO_CREDIT_NEEDED
            key = (cls._match_score(item, query), 1 if free else 0)
            if best_key is None or key > best_key:
                best, best_key = item, key

        if not best or best_key[0] < cls._required_score(query):
            return None
        return best

    @classmethod
    def _meaningful(cls, query: str) -> set:
        return {w for w in query.lower().split()
                if w not in cls._WEAK and len(w) > 2}

    @classmethod
    def _match_score(cls, item: Dict, query: str) -> int:
        """How many meaningful query words appear in the title."""
        title = (item.get("title") or "").lower()
        return sum(1 for w in cls._meaningful(query) if w in title)

    @classmethod
    def _required_score(cls, query: str) -> int:
        """
        A two-word query must match BOTH words, not either one.

        "financial documents" matching only "financial" gave the Toronto
        financial district skyline -- a fine photograph of the wrong thing.
        Longer queries are not held to every word, because a four-word
        subject rarely has a file named after all four.
        """
        wanted = len(cls._meaningful(query))
        if wanted <= 1:
            return 1
        return 2

    @property
    def status(self) -> Dict:
        return {"found": self.found, "misses": self.misses,
                "from_fallback": self.from_fallback,
                "openverse_resting": not self._openverse_awake(),
                "last_error": self.last_error}
