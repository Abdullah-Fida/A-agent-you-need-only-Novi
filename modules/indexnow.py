"""
IndexNow — tells search engines about a new article the moment it publishes.

Without it a new story waits for a crawler to happen by, which for a young
domain is days. With it, Bing, Yandex, Seznam and Naver are notified within
seconds of the article going live. One ping reaches all of them: the engines
share submissions with each other.

Google does not participate. Google still finds articles through the sitemap,
which is why both exist.

Deliberately best-effort. A failed ping must never stop an article being
published — the article is the product, the notification is an optimisation.
"""
import logging
from typing import List, Optional
from urllib.parse import urlparse

logger = logging.getLogger("OmniBot.IndexNow")

ENDPOINT = "https://api.indexnow.org/indexnow"

# The API accepts at most 10,000 URLs per request; nothing here approaches
# that, but a batch is still cheaper than one request per URL.
MAX_URLS = 10_000


class IndexNow:
    """Notifies participating search engines that URLs have changed."""

    def __init__(self, key: str = "", site_url: str = "", db=None):
        self.key = (key or "").strip()
        self.site_url = (site_url or "").rstrip("/")
        self.db = db
        self.pings = 0
        self.last_error = ""

        if not self.key:
            logger.info("INDEXNOW_KEY is not set — new articles will only be "
                        "found by ordinary crawling.")
        elif not self.site_url:
            logger.warning("IndexNow has a key but no site URL; disabled.")

    @property
    def is_ready(self) -> bool:
        return bool(self.key and self.site_url.startswith("http"))

    @property
    def host(self) -> str:
        return urlparse(self.site_url).netloc

    @property
    def key_location(self) -> str:
        """Where the verification file is served from."""
        return f"{self.site_url}/{self.key}.txt"

    async def submit(self, urls: List[str]) -> bool:
        """
        Notifies the engines. Returns True only on an accepted submission.

        Never raises: this runs immediately after a successful publish, and an
        unreachable notification service is not a reason to treat a published
        article as failed.
        """
        if not self.is_ready:
            return False

        # Only URLs on our own host are accepted; anything else makes the whole
        # batch invalid, so a stray link would silently cost every notification.
        clean = [u for u in dict.fromkeys(urls)
                 if u and u.startswith(self.site_url)][:MAX_URLS]
        if not clean:
            return False

        payload = {
            "host": self.host,
            "key": self.key,
            "keyLocation": self.key_location,
            "urlList": clean,
        }

        try:
            import httpx
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(ENDPOINT, json=payload,
                                      headers={"Content-Type": "application/json"})
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.warning(f"IndexNow ping failed: {self.last_error}")
            return False

        # 200 accepted, 202 accepted but the key is still being verified.
        if r.status_code in (200, 202):
            self.pings += len(clean)
            logger.info(f"IndexNow notified of {len(clean)} URL(s) "
                        f"(HTTP {r.status_code}).")
            return True

        # 403 means the key file is missing or does not match, which is a
        # configuration fault worth naming rather than a transient failure.
        if r.status_code == 403:
            self.last_error = (f"IndexNow rejected the key. Check that "
                               f"{self.key_location} is reachable and contains "
                               f"exactly the key.")
            logger.error(self.last_error)
        else:
            self.last_error = f"HTTP {r.status_code}: {r.text[:120]}"
            logger.warning(f"IndexNow returned {self.last_error}")
        return False

    async def submit_article(self, slug: str) -> bool:
        """Notifies about one article, plus the pages that now list it."""
        if not self.is_ready or not slug:
            return False
        return await self.submit([
            f"{self.site_url}/{slug}",
            self.site_url,          # the front page changed too
        ])
