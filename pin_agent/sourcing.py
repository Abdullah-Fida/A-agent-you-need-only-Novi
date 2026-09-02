"""
AliExpress product sourcing.

Talks to the AliExpress Affiliate ("Portals") open platform. Requests are
signed with HMAC-SHA256 over the sorted parameters, which is what that
platform expects; an unsigned request is rejected outright.

Runs on sample data until credentials exist, so filtering, copywriting,
imaging and compliance are all testable end to end before approval lands.
"""
import hashlib
import hmac
import logging
import time
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("PinAgent.Sourcing")

API_URL = "https://api-sg.aliexpress.com/sync"

NICHE_CATEGORIES = {
    "home_kitchen": ["1501", "15"],
    "home_decor": ["1501"],
    "jewellery": ["1509"],
    "phone_accessories": ["509"],
}

# A category id alone returns the same best-selling listings on every call, so
# the deduplication gate would starve within days. These rotate, one per run,
# which is also how the profile stays spread across its boards instead of
# piling forty near-identical drawer dividers onto one.
#
# Every term is a thing somebody types when they have a problem to solve, not
# a thing they browse for. That intent is what makes the category convert.
NICHE_KEYWORDS = {
    "home_kitchen": [
        "kitchen drawer organizer",
        "under sink organizer",
        "spice rack organizer",
        "pantry storage container",
        "fridge organizer bin",
        "cabinet shelf riser",
        "over the sink rack",
        "airtight food container",
        "utensil holder organizer",
        "pot lid organizer rack",
        "stackable storage bin",
        "corner shelf organizer",
        "bathroom storage shelf",
        "wall mounted kitchen rack",
        "closet organizer box",
        "kitchen sink caddy",
    ],
    "home_decor": [
        "wall shelf floating",
        "storage basket woven",
        "desk organizer wood",
        "entryway key holder",
    ],
}


class AliExpressClient:
    """Fetches affiliate products. Falls back to sample data with no keys."""

    def __init__(self, app_key: str = "", app_secret: str = "",
                 tracking_id: str = "", niche: str = "home_kitchen"):
        self.app_key = (app_key or "").strip()
        self.app_secret = (app_secret or "").strip()
        self.tracking_id = (tracking_id or "").strip()
        self.niche = niche
        self.last_error = ""
        self._keyword_index = 0

        if not self.is_live:
            logger.warning("AliExpress credentials missing - running on sample "
                           "products. Apply at portals.aliexpress.com "
                           "(Tools > Dropshipping and Affiliates developer API).")

    @property
    def is_live(self) -> bool:
        return bool(self.app_key and self.app_secret and self.tracking_id)

    def _sign(self, params: Dict[str, str]) -> str:
        """
        HMAC-SHA256 over key+value pairs sorted by key, uppercase hex.

        The platform rejects anything else, and a wrong signature comes back as
        a generic error that reads like bad credentials, so this is kept
        isolated and tested.
        """
        joined = "".join(f"{k}{params[k]}" for k in sorted(params))
        digest = hmac.new(self.app_secret.encode("utf-8"),
                          joined.encode("utf-8"), hashlib.sha256)
        return digest.hexdigest().upper()

    def _build_params(self, method: str, extra: Dict) -> Dict[str, str]:
        params = {
            "app_key": self.app_key,
            "method": method,
            "sign_method": "hmac-sha256",
            "timestamp": str(int(time.time() * 1000)),
            "format": "json",
            "v": "2.0",
        }
        params.update({k: str(v) for k, v in extra.items() if v not in (None, "")})
        params["sign"] = self._sign(params)
        return params

    def next_keywords(self) -> str:
        """
        The next search term for this niche, round-robin.

        Advances even when the query later fails, so a term that returns
        nothing usable cannot pin the agent to itself forever.
        """
        terms = NICHE_KEYWORDS.get(self.niche) or []
        if not terms:
            return ""
        term = terms[self._keyword_index % len(terms)]
        self._keyword_index += 1
        return term

    async def fetch_products(self, keywords: str = "", page: int = 1,
                             page_size: int = 40) -> List[Dict]:
        """Returns normalised product dicts. Never raises."""
        if not self.is_live:
            return self._sample_products()

        # Cleared per call. Left set, a failure from an hour ago makes the
        # empty-result diagnosis below skip itself.
        self.last_error = ""

        categories = NICHE_CATEGORIES.get(self.niche, [])
        params = self._build_params("aliexpress.affiliate.product.query", {
            "keywords": keywords or self.next_keywords(),
            "category_ids": ",".join(categories),
            "page_no": page,
            "page_size": min(page_size, 50),
            "tracking_id": self.tracking_id,
            "target_currency": "USD",
            "target_language": "EN",
            "sort": "LAST_VOLUME_DESC",
        })

        try:
            async with httpx.AsyncClient(timeout=40) as client:
                response = await client.get(API_URL, params=params)
            if response.status_code != 200:
                self.last_error = f"HTTP {response.status_code}"
                logger.error(f"AliExpress returned {self.last_error}")
                return []
            products = self._parse(response.json())
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.error(f"AliExpress request failed: {self.last_error}")
            return []

        if not products and not self.last_error:
            await self._diagnose_empty(keywords)
            return products

        return await self._attach_links(products)

    # ── per-product affiliate links ──────────────────────────────

    # The platform accepts a batch of source URLs. Twenty keeps the query
    # string well inside any limit while making one call do the work of
    # twenty.
    LINK_BATCH = 20

    async def _attach_links(self, products: List[Dict]) -> List[Dict]:
        """
        Replaces the shared promotion link with a real per-product one.

        A search response carries the SAME promotion_link on every row --
        verified live, five products all pointing at
        s.click.aliexpress.com/s/pyFri10M... Publishing that would send
        every pin in a batch to the same page and attribute nothing
        correctly.

        The per-product link comes from aliexpress.affiliate.link.generate.
        Anything that does not come back with one is DROPPED: a pin whose
        link earns nothing is worse than no pin, because it still costs a
        slot and a reader's click.
        """
        wanted = [p for p in products if p.get("detail_url")]
        if not wanted:
            return []

        links: Dict[str, str] = {}
        for start in range(0, len(wanted), self.LINK_BATCH):
            batch = wanted[start:start + self.LINK_BATCH]
            links.update(await self._generate_links(
                [p["detail_url"] for p in batch]))

        kept = []
        for product in wanted:
            link = links.get(product["detail_url"], "")
            if not link:
                continue
            product["affiliate_url"] = link
            kept.append(product)

        missing = len(products) - len(kept)
        if missing:
            logger.warning(f"{missing} product(s) dropped: no affiliate link "
                           f"could be generated.")
        return kept

    async def _generate_links(self, urls: List[str]) -> Dict[str, str]:
        """
        {source url: affiliate link} for one batch.

        Results come back in a DIFFERENT ORDER from the request, so they are
        matched on the echoed source_value. Zipping them positionally would
        silently attach the wrong link to every product -- each one valid,
        each one for the wrong item.
        """
        if not urls:
            return {}
        params = self._build_params("aliexpress.affiliate.link.generate", {
            "promotion_link_type": 0,
            "source_values": ",".join(urls),
            "tracking_id": self.tracking_id,
        })
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                response = await client.get(API_URL, params=params)
            if response.status_code != 200:
                self.last_error = f"link generation HTTP {response.status_code}"
                logger.error(self.last_error)
                return {}
            payload = response.json()
        except Exception as e:
            self.last_error = f"link generation failed: {type(e).__name__}: {e}"
            logger.error(self.last_error)
            return {}

        return self._parse_links(payload)

    @staticmethod
    def _parse_links(payload: Dict) -> Dict[str, str]:
        """
        {source url: affiliate link}, matched on the echoed source_value.

        Separate from the request so the matching can be tested without a
        network call -- it is the part that silently corrupts everything if
        it is wrong.
        """
        node = payload
        for key in ("aliexpress_affiliate_link_generate_response", "resp_result",
                    "result", "promotion_links"):
            if not isinstance(node, dict):
                return {}
            node = node.get(key, {})

        rows = node.get("promotion_link") if isinstance(node, dict) else node
        if not isinstance(rows, list):
            return {}

        out = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            source = str(row.get("source_value") or "").strip()
            link = str(row.get("promotion_link") or "").strip()
            if source and link:
                out[source] = link
        return out

    async def _diagnose_empty(self, keywords: str) -> None:
        """
        Works out WHY a search came back empty.

        A tracking id the platform does not recognise returns HTTP 200, no
        error object and an empty product list -- identical to a search that
        genuinely matched nothing. Found the hard way: the same keyword
        returned five products with the tracking id removed and zero with a
        made-up one.

        Without this the agent would report "no products found" forever
        while the real fault was one wrong string in the environment, so the
        same query is repeated once WITHOUT the tracking id. If that finds
        stock, the tracking id is the problem and says so.
        """
        if not self.tracking_id:
            return
        probe = self._build_params("aliexpress.affiliate.product.query", {
            "keywords": keywords or "kitchen organizer",
            "page_no": 1, "page_size": 3,
            "target_currency": "USD", "target_language": "EN",
        })
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                response = await client.get(API_URL, params=probe)
            if response.status_code != 200:
                return
            # _parse writes to last_error on an error envelope, and a fault
            # in the PROBE must not be reported as the fault in the search.
            found = self._parse(response.json())
            self.last_error = ""
            if found:
                self.last_error = (
                    f"tracking id {self.tracking_id!r} returns no products, but "
                    f"the same search without it does. Check it against "
                    f"portals.aliexpress.com -> Ad Center -> Tracking ID.")
                logger.error(f"AliExpress: {self.last_error}")
        except Exception:
            # A failed diagnosis is not itself an error worth reporting; the
            # caller already knows the search found nothing.
            pass

    def _parse(self, payload: Dict) -> List[Dict]:
        """
        Pulls the product list out of the response.

        The envelope is deeply nested and its shape differs between error and
        success, so every level is treated as optional rather than indexed
        into directly.
        """
        if not isinstance(payload, dict):
            return []

        if "error_response" in payload:
            err = payload["error_response"]
            self.last_error = str(err.get("msg") or err)
            logger.error(f"AliExpress error: {self.last_error}")
            return []

        node = payload
        for key in ("aliexpress_affiliate_product_query_response",
                    "resp_result", "result", "products"):
            if not isinstance(node, dict):
                return []
            node = node.get(key, {})

        items = node.get("product") if isinstance(node, dict) else node
        if not isinstance(items, list):
            return []

        return [p for p in (self._normalise(i) for i in items) if p]

    @staticmethod
    def _normalise(item: Dict) -> Optional[Dict]:
        """Flattens one product into the shape the rest of the agent uses."""
        if not isinstance(item, dict):
            return None

        def num(*keys, default=0.0):
            for k in keys:
                raw = item.get(k)
                if raw not in (None, ""):
                    try:
                        return float(str(raw).replace("%", "").replace(",", ""))
                    except ValueError:
                        continue
            return default

        product_id = str(item.get("product_id") or item.get("productId") or "").strip()
        title = (item.get("product_title") or item.get("productTitle") or "").strip()
        promo = (item.get("promotion_link") or item.get("promotionLink") or "").strip()

        # THE PRODUCT PAGE, kept because the promotion_link that comes back
        # from a search is NOT per-product.
        #
        # Verified against the live API: a search returns the same
        # s.click.aliexpress.com/s/pyFri10M... link on every row, so every
        # pin in a batch would have sent readers to the same place. The
        # per-product link comes from aliexpress.affiliate.link.generate,
        # which needs this URL. Query parameters are stripped because that
        # string is echoed back as `source_value` and is what the results
        # are matched on.
        detail = str(item.get("product_detail_url")
                     or item.get("productDetailUrl") or "").split("?")[0].strip()
        if not product_id or not title or not (promo or detail):
            return None

        images = []
        main = item.get("product_main_image_url") or item.get("productMainImageUrl")
        if main:
            images.append(str(main))
        extra = item.get("product_small_image_urls") or {}
        if isinstance(extra, dict):
            extra = extra.get("string") or []
        if isinstance(extra, list):
            images.extend(str(u) for u in extra if u)

        return {
            "product_id": product_id,
            "title": title,
            "price": num("target_sale_price", "sale_price", "app_sale_price"),
            "original_price": num("target_original_price", "original_price"),
            "rating": num("evaluate_rate", "product_rating", default=0.0),
            "orders": int(num("lastest_volume", "volume", default=0)),
            "commission_rate": num("commission_rate", "hot_product_commission_rate"),
            "category_id": str(item.get("first_level_category_id") or ""),
            "category_name": (item.get("first_level_category_name") or "").strip(),
            "images": images[:6],
            # Replaced with a per-product link by _attach_links(). The
            # shared one is a placeholder, never what gets published.
            "affiliate_url": promo,
            "detail_url": detail,
            "shop_name": (item.get("shop_name") or "").strip(),
        }

    @staticmethod
    def _sample_products() -> List[Dict]:
        """
        Representative products for offline development.

        Deliberately includes bad rows - low rating, too few orders, a missing
        affiliate link, an over-priced item - so the filter and compliance
        stages are exercised rather than always seeing clean input.
        """
        return [
            {"product_id": "1005006123456",
             "title": "Stainless Steel Herb Scissors with 5 Blades and Cleaning Comb",
             "price": 8.99, "original_price": 15.99, "rating": 4.8, "orders": 4210,
             "commission_rate": 8.0, "category_id": "1501", "category_name": "Home & Kitchen",
             "images": ["https://ae01.alicdn.com/kf/sample_herb_scissors.jpg"],
             "affiliate_url": "https://s.click.aliexpress.com/e/_sample1",
             "shop_name": "KitchenPro"},
            {"product_id": "1005006223457",
             "title": "Collapsible Silicone Colander Set Space Saving Kitchen Strainer",
             "price": 12.50, "original_price": 22.00, "rating": 4.6, "orders": 1890,
             "commission_rate": 7.5, "category_id": "1501", "category_name": "Home & Kitchen",
             "images": ["https://ae01.alicdn.com/kf/sample_colander.jpg"],
             "affiliate_url": "https://s.click.aliexpress.com/e/_sample2",
             "shop_name": "HomeEssentials"},
            {"product_id": "1005006323458",
             "title": "Magnetic Spice Rack Wall Mounted Organizer 12 Jars",
             "price": 24.99, "original_price": 39.99, "rating": 4.9, "orders": 780,
             "commission_rate": 9.0, "category_id": "1501", "category_name": "Home & Kitchen",
             "images": ["https://ae01.alicdn.com/kf/sample_spice_rack.jpg"],
             "affiliate_url": "https://s.click.aliexpress.com/e/_sample3",
             "shop_name": "OrganizeIt"},
            {"product_id": "1005006423459", "title": "Cheap Plastic Egg Slicer",
             "price": 2.10, "original_price": 3.00, "rating": 3.4, "orders": 5200,
             "commission_rate": 4.0, "category_id": "1501", "category_name": "Home & Kitchen",
             "images": ["https://ae01.alicdn.com/kf/sample_egg.jpg"],
             "affiliate_url": "https://s.click.aliexpress.com/e/_sample4",
             "shop_name": "CheapStuff"},
            {"product_id": "1005006523460", "title": "Untested Novelty Avocado Tool",
             "price": 6.00, "original_price": 9.00, "rating": 4.9, "orders": 12,
             "commission_rate": 6.0, "category_id": "1501", "category_name": "Home & Kitchen",
             "images": ["https://ae01.alicdn.com/kf/sample_avocado.jpg"],
             "affiliate_url": "https://s.click.aliexpress.com/e/_sample5",
             "shop_name": "NewShop"},
            {"product_id": "1005006623461", "title": "Bamboo Cutting Board Set of 3",
             "price": 19.99, "original_price": 29.99, "rating": 4.7, "orders": 2400,
             "commission_rate": 8.0, "category_id": "1501", "category_name": "Home & Kitchen",
             "images": ["https://ae01.alicdn.com/kf/sample_board.jpg"],
             "affiliate_url": "", "shop_name": "WoodWorks"},
            {"product_id": "1005006723462", "title": "Professional Stand Mixer 1200W",
             "price": 240.00, "original_price": 320.00, "rating": 4.8, "orders": 640,
             "commission_rate": 5.0, "category_id": "1501", "category_name": "Home & Kitchen",
             "images": ["https://ae01.alicdn.com/kf/sample_mixer.jpg"],
             "affiliate_url": "https://s.click.aliexpress.com/e/_sample7",
             "shop_name": "AppliancePro"},
        ]
