"""
The evergreen desk.

News rewrites do not rank. When somebody searches "bitcoin fed inflation",
Google shows CoinDesk -- the outlet that reported it, with a decade of
authority behind it. A young domain covering the same story from the same
wire cannot outrank that, and no amount of publishing volume changes it.

Explainers can rank, because they compete on a different axis. "How does
bitcoin halving affect the price" has no original source to defer to, no
recency to lose, and it keeps pulling search traffic for years. That is the
half of the strategy that actually produces readers.

So this desk works from a bank of long-tail questions rather than from RSS.
Everything downstream -- SEO metadata, the image chain, the quality gate,
IndexNow -- is the article agent's, unchanged.
"""
import logging
import random
from typing import Dict, List, Optional

logger = logging.getLogger("OmniBot.Evergreen")


# Long-tail questions, chosen for the sections the site already covers and
# for competition thin enough that a new domain can reach page one.
#
# The Pakistan entries matter disproportionately: Reuters does not write
# "how are crypto gains taxed in Pakistan", so there is room here that does
# not exist in world news.
TOPIC_BANK: List[Dict[str, str]] = [
    # ── Crypto ────────────────────────────────────────────────────
    {"category": "crypto", "photo": "bitcoin cryptocurrency",
     "title": "How Bitcoin halving affects the price, explained",
     "angle": "the mechanism, the historical pattern, and why past cycles are not a promise"},
    {"category": "crypto", "photo": "hardware wallet",
     "title": "Hot wallet vs cold wallet: which is actually safer",
     "angle": "the real trade-off between convenience and custody, and who each suits"},
    {"category": "crypto", "photo": "data chart",
     "title": "What on-chain analysis is and how to read it",
     "angle": "the handful of metrics worth watching and what they cannot tell you"},
    {"category": "crypto", "photo": "digital currency coins",
     "title": "What happens when a stablecoin loses its peg",
     "angle": "the mechanics of a depeg, why it happens, and what holders can do"},
    {"category": "crypto", "photo": "blockchain network",
     "title": "How crypto gas fees work and why they spike",
     "angle": "block space as an auction, and practical ways to pay less"},
    {"category": "crypto", "photo": "server data centre computing",
     "title": "Proof of work vs proof of stake, in plain English",
     "angle": "what each actually secures, and the energy argument on both sides"},
    {"category": "crypto", "photo": "cryptocurrency trading",
     "title": "How a crypto exchange actually holds your money",
     "angle": "custodial vs non-custodial, proof of reserves, and what a collapse looks like"},
    {"category": "crypto", "photo": "computer code screen",
     "title": "What a smart contract is and how it can fail",
     "angle": "the code-is-law idea, common exploit classes, and audits"},

    # ── Pakistan: thin competition, genuine local knowledge ───────
    {"category": "pakistan", "photo": "tax calculator",
     "title": "How crypto is taxed in Pakistan: what the rules say",
     "angle": "the current legal position, what is unresolved, and what records to keep"},
    {"category": "pakistan", "photo": "stock exchange trading floor",
     "title": "How to start investing in the Pakistan Stock Exchange",
     "angle": "the account, the costs, and the first decisions a beginner faces"},
    {"category": "pakistan", "photo": "money transfer banking",
     "title": "How remittances to Pakistan actually work",
     "angle": "the routes money takes, what each costs, and where the fees hide"},
    {"category": "pakistan", "photo": "office workspace",
     "title": "Freelancing from Pakistan: getting paid from abroad",
     "angle": "the payment rails available, their limits, and the tax position"},
    {"category": "pakistan", "photo": "karachi city pakistan",
     "title": "Why the Pakistani rupee moves against the dollar",
     "angle": "the drivers of the exchange rate and what a devaluation does to prices"},
    {"category": "pakistan", "photo": "solar panels rooftop",
     "title": "Is rooftop solar worth it in Pakistan",
     "angle": "the arithmetic of payback, net metering, and the failure modes"},

    # ── Business ──────────────────────────────────────────────────
    {"category": "business_markets", "photo": "inflation money currency",
     "title": "What causes inflation, explained simply",
     "angle": "demand, supply and money supply, and why economists disagree"},
    {"category": "business_markets", "photo": "central bank building",
     "title": "Why central banks raise interest rates",
     "angle": "the transmission mechanism from a rate decision to household prices"},
    {"category": "business_markets", "photo": "financial report documents",
     "title": "How to read a company earnings report",
     "angle": "the four numbers that matter and the ones designed to distract"},
    {"category": "business_markets", "photo": "shipping containers port",
     "title": "How a supply chain actually breaks",
     "angle": "the chokepoints, the bullwhip effect, and why shortages outlast the cause"},
    {"category": "business_markets", "photo": "stock market",
     "title": "What a recession is and how one is declared",
     "angle": "the definitions, who decides, and the indicators watched in advance"},

    # ── Tech ──────────────────────────────────────────────────────
    {"category": "tech_ai", "photo": "artificial intelligence technology",
     "title": "How to spot an AI-generated image",
     "angle": "the artefacts that still give it away, and why they keep shrinking"},
    {"category": "tech_ai", "photo": "source code",
     "title": "What a context window is and why it limits AI",
     "angle": "what the model can hold at once, and what that means in practice"},
    {"category": "tech_ai", "photo": "password security lock",
     "title": "How password managers work and whether to trust one",
     "angle": "the encryption model, the real risks, and the alternative"},
    {"category": "tech_ai", "photo": "mobile phone",
     "title": "What a VPN does, and what it does not",
     "angle": "the threat model it addresses and the widespread claims that are false"},
    {"category": "tech_ai", "photo": "cloud computing servers",
     "title": "What happens to your data in the cloud",
     "angle": "where files physically sit, who can read them, and encryption at rest"},
    {"category": "tech_ai", "photo": "electric vehicle charging",
     "title": "How EV batteries degrade, and what actually helps",
     "angle": "the chemistry of capacity loss and the habits that slow it"},

    # ── World ─────────────────────────────────────────────────────
    {"category": "world_news", "photo": "international politics flags",
     "title": "How sanctions work and why they often fail",
     "angle": "the mechanisms, the leakage, and the historical record"},
    {"category": "world_news", "photo": "dollar bills",
     "title": "Why currencies collapse, and what follows",
     "angle": "the common precursors and how recoveries have been engineered"},
    {"category": "world_news", "photo": "wheat field agriculture",
     "title": "How a food crisis begins",
     "angle": "the interaction of weather, export bans and fertiliser prices"},
    {"category": "world_news", "photo": "oil refinery industry",
     "title": "What actually sets the oil price",
     "angle": "OPEC quotas, futures markets, and why pump prices lag"},
]




class EvergreenDesk:
    """Publishes one explainer at a time, from the topic bank."""

    def __init__(self, article_agent=None, db=None, brain=None,
                 photos=None):
        self.article_agent = article_agent
        self.db = db
        self.brain = brain
        # Real photography. An explainer has no wire photo, and a
        # generated illustration is visibly synthetic on a news page.
        self.photos = photos
        self.published = 0
        self.last_error = ""

    async def _already_written(self, title: str) -> bool:
        """
        Whether this topic already has an article.

        Matched on the evergreen marker in source_name plus the topic title,
        because an explainer has no source URL to key on the way a news story
        does.
        """
        if not self.db:
            return False
        try:
            import asyncio
            client = getattr(self.db, "client", None)
            if client is None:
                return False
            res = await asyncio.to_thread(
                lambda: client.table("articles").select("slug")
                .eq("source_url", self._topic_key(title)).limit(1).execute())
            return bool(res.data)
        except Exception as e:
            logger.warning(f"Evergreen duplicate check failed: {type(e).__name__}: {e}")
            return False

    @staticmethod
    def _topic_key(title: str) -> str:
        """
        A stable identifier for a topic, stored in source_url.

        Explainers have no external source, but the column is the one place
        deduplication already looks, and an internal scheme keeps that single
        mechanism rather than adding a second one.
        """
        slug = "".join(c if c.isalnum() else "-" for c in title.lower())
        return "evergreen:" + "-".join(p for p in slug.split("-") if p)[:120]

    async def next_topic(self) -> Optional[Dict[str, str]]:
        """The next unwritten topic, or None when the bank is exhausted."""
        for topic in random.sample(TOPIC_BANK, len(TOPIC_BANK)):
            if not await self._already_written(topic["title"]):
                return topic
        logger.info("Every evergreen topic in the bank has been written.")
        return None

    async def publish_one(self) -> Optional[Dict]:
        """Writes and publishes a single explainer."""
        if not self.article_agent:
            return None
        if self.brain and not getattr(self.brain, "website_module_active", False):
            logger.info("Website module is OFF — skipping the evergreen article.")
            return None

        topic = await self.next_topic()
        if not topic:
            return None

        logger.info(f"Evergreen desk writing: '{topic['title']}'")

        # Presented to the article agent as a story so the whole downstream
        # pipeline -- SEO, images, quality gate, IndexNow -- is shared.
        photo_url, credit = "", ""
        if self.photos:
            found, credit = await self.photos.find(topic.get("photo", topic["title"]))
            photo_url = found or ""

        story = {
            "title": topic["title"],
            "summary": topic["angle"],
            "link": self._topic_key(topic["title"]),
            "source": "",
            # A real openly-licensed photograph. Falls through to the
            # generator only when nothing usable was found.
            "real_image_url": photo_url,
            "evergreen": True,
            # Printed under the article when the licence requires it. An
            # uncredited CC-BY image is a licence breach, not an oversight.
            "image_credit": credit,
        }

        try:
            article = await self.article_agent.generate_and_publish_article(
                story=story, category=topic["category"])
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.error(f"Evergreen article failed: {self.last_error}")
            return None

        if article:
            self.published += 1
            logger.info(f"Evergreen published: /{article.get('slug')}")
        else:
            self.last_error = getattr(self.article_agent, "last_skip_reason", "unknown")
            logger.warning(f"Evergreen not published: {self.last_error}")
        return article

    @property
    def status(self) -> Dict:
        return {
            "topics_in_bank": len(TOPIC_BANK),
            "published_this_run": self.published,
            "last_error": self.last_error,
        }
