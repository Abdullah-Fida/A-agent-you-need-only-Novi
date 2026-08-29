"""
Product filtering and ranking.

Two jobs: throw out products that should never be promoted, and rank what
survives. The ranking is where the analytics feedback loop plugs in — once
real performance data exists, categories and price bands that actually earn
are weighted up rather than everything being ranked on order count alone.
"""
import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger("PinAgent.Selector")

# Words that mark a listing as unsuitable to promote. Counterfeits and medical
# claims are the two categories most likely to get an affiliate account closed,
# and adult or weapon items breach Pinterest's content policy outright.
BANNED_TERMS = [
    "replica", "copy brand", "fake", "knockoff", "unauthorized",
    "cure", "treat cancer", "medical grade", "fda approved",
    "weight loss", "slimming", "detox",
    "vape", "e-cigarette", "tobacco", "cbd",
    "knife weapon", "taser", "pepper spray", "handcuff",
    "adult toy", "sex", "lingerie",
    "airpod", "iphone case for", "nike", "adidas", "gucci", "louis vuitton",
    "disney", "marvel", "pokemon", "hello kitty",
]

# Listing titles are keyword soup. These get stripped before the copywriter
# sees the title, so the model is not fed "2024 New Hot Sale Dropshipping".
TITLE_NOISE = [
    "free shipping", "hot sale", "new arrival", "dropshipping", "wholesale",
    "high quality", "best price", "2023", "2024", "2025", "2026",
    "drop shipping", "in stock", "fast delivery", "on sale",
]


class ProductSelector:
    """Applies the hard filters, then ranks what is left."""

    def __init__(self, min_rating: float = 4.3, min_orders: int = 100,
                 min_price: float = 3.0, max_price: float = 80.0,
                 performance: Optional[Dict[str, float]] = None):
        self.min_rating = min_rating
        self.min_orders = min_orders
        self.min_price = min_price
        self.max_price = max_price
        # category_name -> multiplier, learned from real pin performance.
        self.performance = performance or {}
        self.rejections: Dict[str, int] = {}

    # ── filtering ────────────────────────────────────────────────

    def _reject(self, reason: str) -> None:
        self.rejections[reason] = self.rejections.get(reason, 0) + 1

    def is_eligible(self, product: Dict) -> bool:
        """
        Whether this product may be promoted at all.

        Order count is the important one: a high rating on eleven orders says
        nothing, while thousands of orders is the only real evidence that the
        listing ships and is not abandoned.
        """
        if not product.get("affiliate_url", "").startswith("http"):
            self._reject("no affiliate link")
            return False

        if not product.get("images"):
            self._reject("no image")
            return False

        title = (product.get("title") or "").lower()
        if len(title) < 15:
            self._reject("title too short")
            return False

        for term in BANNED_TERMS:
            if term in title:
                self._reject(f"banned term: {term}")
                return False

        if product.get("rating", 0) < self.min_rating:
            self._reject("rating too low")
            return False

        if product.get("orders", 0) < self.min_orders:
            self._reject("too few orders")
            return False

        price = product.get("price", 0)
        if price < self.min_price:
            self._reject("price below floor")
            return False
        if price > self.max_price:
            self._reject("price above ceiling")
            return False

        return True

    # ── ranking ──────────────────────────────────────────────────

    def score(self, product: Dict) -> float:
        """
        Higher is better.

        Order count is compressed logarithmically: the gap between 100 and
        1000 orders matters far more than between 9000 and 10000, and without
        compression a single viral listing would dominate every batch forever.
        """
        import math

        orders = max(1, product.get("orders", 0))
        score = math.log10(orders) * 10          # 100 orders = 20, 10k = 40

        score += (product.get("rating", 0) - self.min_rating) * 12
        score += min(product.get("commission_rate", 0), 15) * 1.5

        # A visible discount gives the pin a reason to exist today.
        original = product.get("original_price", 0)
        price = product.get("price", 0)
        if original > price > 0:
            discount = (original - price) / original
            score += min(discount, 0.7) * 20

        # Mid-range prices convert best: very cheap items feel disposable,
        # expensive ones are not impulse buys from a pin.
        if 8 <= price <= 35:
            score += 8

        # Learned preference from real pin performance.
        category = (product.get("category_name") or "").lower()
        score *= self.performance.get(category, 1.0)

        return round(score, 2)

    def select(self, products: List[Dict], limit: int = 10,
               exclude_ids: Optional[set] = None) -> List[Dict]:
        """Returns the best eligible products, best first."""
        exclude_ids = exclude_ids or set()
        self.rejections = {}

        eligible = []
        for product in products:
            if str(product.get("product_id")) in exclude_ids:
                self._reject("already posted")
                continue
            if self.is_eligible(product):
                product = dict(product)
                product["score"] = self.score(product)
                product["clean_title"] = self.clean_title(product.get("title", ""))
                eligible.append(product)

        eligible.sort(key=lambda p: p["score"], reverse=True)

        if self.rejections:
            summary = ", ".join(f"{k}: {v}" for k, v in sorted(self.rejections.items()))
            logger.info(f"Filtered {len(products)} products down to "
                        f"{len(eligible)} ({summary})")
        return eligible[:limit]

    # ── title cleanup ────────────────────────────────────────────

    @staticmethod
    def clean_title(title: str) -> str:
        """
        Turns a keyword-stuffed listing title into something readable.

        Fed to the copywriter as context. Left raw, the model picks up the
        seller's SEO spam and writes "2024 New Hot Sale Herb Scissors".
        """
        text = (title or "").strip()
        text = re.sub(r"[\[\(][^\])]*[\])]", " ", text)      # bracketed noise
        for noise in TITLE_NOISE:
            text = re.sub(re.escape(noise), " ", text, flags=re.I)
        text = re.sub(r"[^\w\s\-&/,.]", " ", text)
        text = re.sub(r"\s+", " ", text).strip(" -,.")

        words = text.split()
        if len(words) > 12:
            text = " ".join(words[:12])
        return text or "Kitchen Gadget"
