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

# Claims that stop being true while the pin is still being seen. A permanent
# quality is fine -- "affordable", "budget-friendly" -- because it does not
# expire. A NUMBER does, and so does a sale.
_PRICE_CLAIM = re.compile(
    r"[$£€¥₹]\s?\d"                                        # $20, €9.99
    r"|\b\d[\d,.]*\s?(?:usd|eur|gbp|pkr|aud|cad)\b"        # 20 USD
    r"|\b(?:usd|eur|gbp|pkr|aud|cad)\s?\d"                  # USD 20
    r"|\b\d[\d,.]*\s?(?:dollars?|euros?|pounds?|bucks?|rupees?)\b"
    r"|\b\d{1,3}\s?(?:%|percent)\s?(?:off|discount|cheaper|less)\b"
    r"|\bhalf[\s-]price\b"
    # "clearance" needs the sale context. On its own it is an ordinary word
    # in this niche and it was refusing good advice: "Place a shallow bin
    # under the sink for cleaning supplies" was blocked because its body
    # said there was enough clearance above the pipes. Same for "markdown",
    # which is a text format as often as a discount.
    r"|\bclearance\s+(?:sale|price|event|deal|rack|section)\b"
    r"|\b(?:on|for)\s+clearance\b"
    r"|\b(?:on sale|sale price|flash sale"
    r"|lowest price|best price|cheapest)\b",
    re.I,
)

# Any of these in a destination URL means the affiliate link was wrapped or
# shortened. Pinterest treats that as cloaking and flags the account.
SHORTENER_HOSTS = {
    "bit.ly", "tinyurl.com", "goo.gl", "ow.ly", "t.co", "buff.ly",
    "rebrand.ly", "cutt.ly", "shorturl.at", "is.gd", "s.id", "rb.gy",
    "linktr.ee", "bio.link", "shorte.st", "adf.ly",
}

# The disclosure Pinterest and the FTC both expect. Any one is enough.
DISCLOSURE_MARKERS = ("#ad", "#affiliate", "#affiliatelink", "#sponsored")

# The `angle` recorded for an advice pin. Kept here as well as in the store
# because the gate has to recognise one that came back out of the database,
# where `kind` does not survive but `angle` does.
VALUE_ANGLE = "tip"

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
    def check_no_link(pin: Dict) -> Optional[str]:
        """
        An advice pin must carry nothing to sell. Returns a failure reason.

        The inverse of check_link, and the more important of the two. An
        advice pin is published with no destination URL and no #ad, because
        there is nothing to disclose -- so if a link ever leaked into one,
        the result would be an UNDISCLOSED affiliate pin, which is worse
        than anything this gate was originally written to stop.
        """
        if (pin.get("link") or "").strip():
            return "an advice pin must carry no destination link"

        text = (pin.get("description") or "").lower()
        leaked = [m for m in DISCLOSURE_MARKERS if m in text]
        if leaked:
            # Not a disclosure problem but an identity one: #ad on a pin
            # that sells nothing means product copy has ended up on an
            # advice pin, so the pin is not what the pipeline thinks it is.
            return (f"an advice pin carries a disclosure marker "
                    f"({leaked[0]}), so it is not advice")
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

        # Anything that GOES STALE is refused. AliExpress prices move
        # constantly and a pin outlives them by months, so a stated price --
        # or a discount, or "on sale" -- stops being true long before the pin
        # stops being seen, and an untrue claim on an affiliate pin is the
        # thing that actually costs an account.
        #
        # The old check was a currency symbol followed by a digit, which let
        # "20 USD", "50% off" and "half price" straight through.
        stale = _PRICE_CLAIM.search(f"{title} {description}")
        if stale:
            return (f"the copy states something that will go stale "
                    f"({stale.group(0).strip()!r})")
        return None

    # ── dedupe ───────────────────────────────────────────────────

    @staticmethod
    def image_fingerprint(image_bytes: bytes) -> str:
        return hashlib.sha256(image_bytes or b"").hexdigest()[:32]

    def check_duplicate(self, product_id: str, url: str,
                        image_hash: str = "",
                        check_product: bool = True) -> Optional[str]:
        """
        AliExpress relists the same item under new ids constantly, so identity
        is checked three ways rather than trusting the product id alone.

        `check_product` is off for advice pins. Their id is a tip slug, and
        the bank holds sixty tips against a rotation window of fifty -- so
        every tip is meant to return in time. Burning the id for 120 days
        the way a relisted product is burned would empty the bank inside a
        fortnight and leave the agent with nothing to publish. The IMAGE
        hash still applies to them, because a byte-identical pin twice is
        the failure anyone would actually see.
        """
        if check_product and product_id and str(product_id) in self.seen_products:
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

    @staticmethod
    def kind_of(pin: Dict) -> str:
        """
        "value" for an advice pin, "product" for an affiliate one.

        INFERRED FROM THE ANGLE when the key is absent, and that matters. A
        pin that has sat in the review queue is reloaded from pin_posts,
        which has columns but no `kind` -- so a restored advice pin would
        arrive looking like a product pin, be judged against the product
        rules, and fail for having no link. The angle survives the round
        trip because it is a real column.
        """
        if pin.get("kind"):
            return str(pin["kind"])
        return "value" if (pin.get("angle") or "") == VALUE_ANGLE else "product"

    def approve(self, pin: Dict) -> Tuple[bool, List[str]]:
        """
        Runs every rule. Returns (ok, reasons) with all failures, not just the
        first, so a rejected pin can be fixed in one pass.

        TWO SETS OF RULES, because there are now two sorts of pin. Advice
        pins exist because an account where every single pin sells something
        is the pattern Pinterest suppresses; they carry no link, so the link
        and disclosure rules are replaced by their opposite rather than
        skipped. Everything else -- length, stale claims, duplicates, an
        image -- applies to both.
        """
        value = self.kind_of(pin) == "value"

        reasons = [
            reason for reason in (
                self.check_no_link(pin) if value
                else self.check_link(pin.get("link", "")),
                None if value
                else self.check_disclosure(pin.get("description", "")),
                self.check_text(pin.get("title", ""), pin.get("description", "")),
                self.check_duplicate(pin.get("product_id", ""),
                                     pin.get("link", ""),
                                     pin.get("image_hash", ""),
                                     # The tip bank rotates by title, and a
                                     # tip is meant to come round again
                                     # eventually. Its id is not an identity
                                     # to burn for 120 days the way a
                                     # relisted AliExpress product is.
                                     check_product=not value),
            ) if reason
        ]
        if not pin.get("image_path") and not pin.get("image_url"):
            reasons.append("no image attached")

        if reasons:
            logger.warning(f"Pin rejected for '{pin.get('title', '')[:40]}': "
                           + "; ".join(reasons))
        return (not reasons), reasons
