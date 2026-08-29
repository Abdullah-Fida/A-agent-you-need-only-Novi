"""
Compliance gate.

Every check here is plain code, never a model decision. Pinterest bans
shortened or cloaked affiliate links and requires a visible disclosure, and
an account lost to a spam flag is not recoverable by apologising — so these
rules are enforced deterministically and a pin that fails any of them is not
publishable, full stop.

Deliberately kept free of AI: asking a language model "is this compliant?"
gives an answer that is usually right, and the failure mode is losing the
account.
"""
import hashlib
import logging
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("PinAgent.Compliance")

# Any of these in a destination URL means the affiliate link was wrapped or
# shortened. Pinterest treats that as cloaking and flags the account.
SHORTENER_HOSTS = {
    "bit.ly", "tinyurl.com", "goo.gl", "ow.ly", "t.co", "buff.ly",
    "rebrand.ly", "cutt.ly", "shorturl.at", "is.gd", "s.id", "rb.gy",
    "linktr.ee", "bio.link", "shorte.st", "adf.ly",
}

# The disclosure Pinterest and the FTC both expect. Any one is enough.
DISCLOSURE_MARKERS = ("#ad", "#affiliate", "#affiliatelink", "#sponsored")

# Hosts an AliExpress affiliate link legitimately uses.
ALLOWED_LINK_HOSTS = {
    "s.click.aliexpress.com", "aliexpress.com", "www.aliexpress.com",
    "a.aliexpress.com", "star.aliexpress.com",
}

MAX_TITLE = 100          # Pinterest truncates beyond this
MAX_DESCRIPTION = 800


class ComplianceGate:
    """Decides whether a prepared pin may be published."""

    def __init__(self, allowed_hosts: Optional[set] = None):
        self.allowed_hosts = allowed_hosts or ALLOWED_LINK_HOSTS
        # Fingerprints of what has already gone out, so the same product or
        # the same picture is not pinned twice.
        self.seen_products: set = set()
        self.seen_images: set = set()
        self.seen_urls: set = set()

    # ── individual rules ─────────────────────────────────────────

    @staticmethod
    def _host(url: str) -> str:
        match = re.match(r"https?://([^/:?#]+)", (url or "").strip(), re.I)
        return match.group(1).lower() if match else ""

    def check_link(self, url: str) -> Optional[str]:
        """Returns a failure reason, or None if the link is acceptable."""
        url = (url or "").strip()
        if not url:
            return "no destination link"
        if not url.lower().startswith("https://"):
            return "link is not https"

        host = self._host(url)
        if not host:
            return "link has no host"
        if host in SHORTENER_HOSTS:
            return f"shortened link ({host}) - Pinterest treats this as cloaking"
        # Exact host, or a subdomain of one. Matching on the registrable
        # suffix instead would accept any .com address, which is how
        # sketchy-redirect.com passed this check.
        if not any(host == allowed or host.endswith("." + allowed)
                   for allowed in self.allowed_hosts):
            return f"unexpected link host: {host}"
        return None

    @staticmethod
    def check_disclosure(description: str) -> Optional[str]:
        """The affiliate relationship must be visible in the description."""
        text = (description or "").lower()
        if not any(marker in text for marker in DISCLOSURE_MARKERS):
            return "no affiliate disclosure (#ad) in the description"
        return None

    @staticmethod
    def check_text(title: str, description: str) -> Optional[str]:
        title = (title or "").strip()
        description = (description or "").strip()
        if len(title) < 10:
            return "title too short"
        if len(title) > MAX_TITLE:
            return f"title over {MAX_TITLE} characters"
        if len(description) < 40:
            return "description too short"
        if len(description) > MAX_DESCRIPTION:
            return f"description over {MAX_DESCRIPTION} characters"

        # A price in the copy goes stale: AliExpress prices move constantly and
        # a pin outlives them by months, so a stated price becomes a false
        # claim rather than a selling point.
        if re.search(r"[$£€]\s?\d", title + " " + description):
            return "price stated in the copy - it will go stale and mislead"
        return None

    # ── dedupe ───────────────────────────────────────────────────

    @staticmethod
    def image_fingerprint(image_bytes: bytes) -> str:
        return hashlib.sha256(image_bytes or b"").hexdigest()[:32]

    def check_duplicate(self, product_id: str, url: str,
                        image_hash: str = "") -> Optional[str]:
        """
        AliExpress relists the same item under new ids constantly, so identity
        is checked three ways rather than trusting the product id alone.
        """
        if product_id and str(product_id) in self.seen_products:
            return "this product has already been pinned"
        if url and url in self.seen_urls:
            return "this destination link has already been pinned"
        if image_hash and image_hash in self.seen_images:
            return "this exact image has already been pinned"
        return None

    def remember(self, product_id: str, url: str, image_hash: str = "") -> None:
        if product_id:
            self.seen_products.add(str(product_id))
        if url:
            self.seen_urls.add(url)
        if image_hash:
            self.seen_images.add(image_hash)

    def load_history(self, product_ids: List[str], urls: List[str],
                     image_hashes: List[str]) -> None:
        """Seeds the dedupe sets from the database on startup."""
        self.seen_products = {str(p) for p in product_ids if p}
        self.seen_urls = {u for u in urls if u}
        self.seen_images = {h for h in image_hashes if h}
        logger.info(f"Dedupe history loaded: {len(self.seen_products)} products, "
                    f"{len(self.seen_urls)} links, {len(self.seen_images)} images.")

    # ── the gate ─────────────────────────────────────────────────

    def approve(self, pin: Dict) -> Tuple[bool, List[str]]:
        """
        Runs every rule. Returns (ok, reasons) with all failures, not just the
        first, so a rejected pin can be fixed in one pass.
        """
        reasons = [
            reason for reason in (
                self.check_link(pin.get("link", "")),
                self.check_disclosure(pin.get("description", "")),
                self.check_text(pin.get("title", ""), pin.get("description", "")),
                self.check_duplicate(pin.get("product_id", ""),
                                     pin.get("link", ""),
                                     pin.get("image_hash", "")),
            ) if reason
        ]
        if not pin.get("image_path") and not pin.get("image_url"):
            reasons.append("no image attached")

        if reasons:
            logger.warning(f"Pin rejected for '{pin.get('title', '')[:40]}': "
                           + "; ".join(reasons))
        return (not reasons), reasons
