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
    # ── CRYPTO ──────────────────────────────────────────────
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
    {"category": "crypto", "photo": "source code",
     "title": "What a smart contract is and how it can fail",
     "angle": "the code-is-law idea, common exploit classes, and audits"},
    {"category": "crypto", "photo": "bitcoin mining",
     "title": "How Bitcoin mining actually works",
     "angle": "the puzzle, the difficulty adjustment, and where the electricity goes"},
    {"category": "crypto", "photo": "cryptocurrency",
     "title": "What a crypto bull run is and how they end",
     "angle": "the pattern of past cycles and the signals that preceded each top"},
    {"category": "crypto", "photo": "computer security",
     "title": "How crypto scams work, and the tells",
     "angle": "rug pulls, approval drains and impersonation, with what to check first"},
    {"category": "crypto", "photo": "financial documents",
     "title": "What a crypto whitepaper should contain",
     "angle": "how to read one critically and the omissions that matter"},
    {"category": "crypto", "photo": "network cables",
     "title": "What Layer 2 means and why it exists",
     "angle": "the scaling problem, rollups, and the trade-offs each makes"},
    {"category": "crypto", "photo": "digital art",
     "title": "What NFTs actually are, beyond the pictures",
     "angle": "ownership, provenance, and the uses that outlived the hype"},
    {"category": "crypto", "photo": "bank vault",
     "title": "What custody means in crypto and why it matters",
     "angle": "not your keys not your coins, and what institutional custody adds"},
    {"category": "crypto", "photo": "stock market",
     "title": "How crypto derivatives work and why they move spot",
     "angle": "perpetuals, funding rates and liquidation cascades"},
    {"category": "crypto", "photo": "coins money",
     "title": "What a token burn does to supply",
     "angle": "deflationary mechanics and when they change nothing"},
    {"category": "crypto", "photo": "internet technology",
     "title": "How a crypto bridge works and why they get hacked",
     "angle": "locking and minting, and the trust assumption in the middle"},
    {"category": "crypto", "photo": "voting ballot",
     "title": "How DAO governance actually functions",
     "angle": "proposals, quorum, delegation, and why turnout is usually low"},
    {"category": "crypto", "photo": "gold bars",
     "title": "Is Bitcoin really digital gold",
     "angle": "the scarcity argument, the correlation record, and the disagreement"},
    # ── PAKISTAN ────────────────────────────────────────────
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
    {"category": "pakistan", "photo": "electricity power lines",
     "title": "Why electricity costs what it does in Pakistan",
     "angle": "capacity payments, circular debt, and what actually sets the tariff"},
    {"category": "pakistan", "photo": "wheat field agriculture",
     "title": "How wheat pricing works in Pakistan",
     "angle": "support prices, procurement, and why shortages recur"},
    {"category": "pakistan", "photo": "bank building",
     "title": "How to open a foreign currency account in Pakistan",
     "angle": "the account types, the restrictions, and what they are useful for"},
    {"category": "pakistan", "photo": "shipping containers port",
     "title": "What Pakistan actually imports and exports",
     "angle": "the trade balance, the concentration risk, and the currency link"},
    {"category": "pakistan", "photo": "students classroom",
     "title": "How to fund university study abroad from Pakistan",
     "angle": "the routes, the proof-of-funds problem, and the timelines"},
    {"category": "pakistan", "photo": "property houses",
     "title": "How property is bought and registered in Pakistan",
     "angle": "the steps, the taxes, and the verification that prevents disputes"},
    {"category": "pakistan", "photo": "mobile phone",
     "title": "How mobile banking changed money in Pakistan",
     "angle": "the rise of wallets, what they solved, and what they did not"},
    {"category": "pakistan", "photo": "inflation money currency",
     "title": "Why inflation in Pakistan outpaces wages",
     "angle": "the components of the basket and the mechanism behind the gap"},
    # ── BUSINESS ────────────────────────────────────────────
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
    {"category": "business_markets", "photo": "stock exchange trading floor",
     "title": "What an IPO is and who actually benefits",
     "angle": "the process, the pricing, and the lock-up that follows"},
    {"category": "business_markets", "photo": "calculator finance",
     "title": "How compound interest really works",
     "angle": "the arithmetic, the time factor, and why it cuts both ways"},
    {"category": "business_markets", "photo": "office building",
     "title": "What private equity does to a company",
     "angle": "the leveraged buyout, the cost cuts, and the exit"},
    {"category": "business_markets", "photo": "dollar bills",
     "title": "Why the US dollar dominates world trade",
     "angle": "reserve currency status, the network effect, and the challenges to it"},
    {"category": "business_markets", "photo": "oil refinery industry",
     "title": "How commodities are priced and traded",
     "angle": "spot versus futures, contango, and who the participants are"},
    {"category": "business_markets", "photo": "warehouse logistics",
     "title": "What just-in-time inventory really costs",
     "angle": "the efficiency case and the fragility it introduced"},
    {"category": "business_markets", "photo": "handshake meeting",
     "title": "How company valuation actually works",
     "angle": "multiples, discounted cash flow, and where the assumptions hide"},
    {"category": "business_markets", "photo": "credit cards",
     "title": "How credit scores are calculated",
     "angle": "the inputs, their weights, and the myths that persist"},
    {"category": "business_markets", "photo": "bank vault",
     "title": "What a bank run is and how they are stopped",
     "angle": "the mechanics, deposit insurance, and the speed social media added"},
    {"category": "business_markets", "photo": "graph chart",
     "title": "What GDP measures, and what it misses",
     "angle": "the components, the revisions, and the critiques"},
    # ── TECH ────────────────────────────────────────────────
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
    {"category": "tech_ai", "photo": "computer keyboard",
     "title": "How two-factor authentication actually protects you",
     "angle": "what each factor stops, and why SMS is the weakest"},
    {"category": "tech_ai", "photo": "network cables",
     "title": "What end-to-end encryption really means",
     "angle": "who can read what, and the metadata it does not hide"},
    {"category": "tech_ai", "photo": "robot technology",
     "title": "How machine learning differs from traditional software",
     "angle": "learned behaviour versus written rules, and what that changes"},
    {"category": "tech_ai", "photo": "satellite space",
     "title": "How GPS actually knows where you are",
     "angle": "the satellites, the timing problem, and the accuracy limits"},
    {"category": "tech_ai", "photo": "solar panels rooftop",
     "title": "How solar panels convert light to electricity",
     "angle": "the photovoltaic effect, efficiency limits, and degradation"},
    {"category": "tech_ai", "photo": "computer chip",
     "title": "Why semiconductor manufacturing is so hard",
     "angle": "the physics, the capital cost, and the concentration of supply"},
    {"category": "tech_ai", "photo": "wifi router",
     "title": "How your internet connection actually reaches you",
     "angle": "the last mile, peering, and where congestion really happens"},
    {"category": "tech_ai", "photo": "smartphone",
     "title": "What phone makers mean by camera megapixels",
     "angle": "sensor size, pixel binning, and why more is not better"},
    {"category": "tech_ai", "photo": "data centre",
     "title": "What cloud outages teach about redundancy",
     "angle": "single points of failure and multi-region trade-offs"},
    {"category": "tech_ai", "photo": "email screen",
     "title": "How spam filters decide what you see",
     "angle": "the signals, the false positives, and why legitimate mail is lost"},
    # ── WORLD ───────────────────────────────────────────────
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
    {"category": "world_news", "photo": "united nations building",
     "title": "How the UN Security Council actually works",
     "angle": "the veto, the practical limits, and the reform debate"},
    {"category": "world_news", "photo": "cargo ship ocean",
     "title": "Why shipping chokepoints matter so much",
     "angle": "Suez, Hormuz and Malacca, and what a closure does to prices"},
    {"category": "world_news", "photo": "refugee camp",
     "title": "How refugee status is legally determined",
     "angle": "the convention, the process, and where it breaks down"},
    {"category": "world_news", "photo": "climate weather",
     "title": "How climate targets are actually measured",
     "angle": "baselines, offsets, and why the numbers are contested"},
    {"category": "world_news", "photo": "election voting",
     "title": "How election observers decide if a vote was fair",
     "angle": "the methodology and its limits"},
    {"category": "world_news", "photo": "water reservoir",
     "title": "Why water disputes between countries are so hard",
     "angle": "upstream rights, treaties, and enforcement"},
    {"category": "world_news", "photo": "hospital medical",
     "title": "How a disease outbreak becomes a pandemic",
     "angle": "the thresholds, the declaration, and what changes when it is made"},
    {"category": "world_news", "photo": "power plant energy",
     "title": "Why energy independence is harder than it sounds",
     "angle": "grid realities, import dependence, and the transition cost"},
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

    # Below this many unwritten topics, the desk starts inventing its own.
    # Set high enough that replenishment happens quietly in the background
    # rather than as an emergency on the day the bank empties.
    LOW_STOCK = 6

    async def remaining(self) -> int:
        """How many curated topics are still unwritten."""
        n = 0
        for topic in TOPIC_BANK:
            if not await self._already_written(topic["title"]):
                n += 1
        return n

    async def next_topic(self) -> Optional[Dict[str, str]]:
        """
        The next topic to write.

        Prefers the curated bank, which is hand-checked for search demand and
        for competition thin enough to rank in. When that runs low the desk
        invents its own rather than stopping -- 77 curated topics is 38 days
        at two a day, and a content pipeline that silently halts after five
        weeks is not a pipeline.
        """
        for topic in random.sample(TOPIC_BANK, len(TOPIC_BANK)):
            if not await self._already_written(topic["title"]):
                left = await self.remaining()
                if left <= self.LOW_STOCK:
                    logger.warning(f"Evergreen bank is down to {left} unwritten "
                                   f"topics; inventing new ones from here.")
                return topic

        logger.info("Curated bank exhausted; asking the model for a new topic.")
        return await self._invent_topic()

    async def _invent_topic(self) -> Optional[Dict[str, str]]:
        """
        Generates a fresh explainer topic in the same style as the bank.

        Deliberately narrow: it is told the sections, told to produce a
        question somebody would actually type, and told to avoid the titles
        already used. A duplicate is rejected rather than published, because
        two articles answering the same question compete with each other in
        search rather than adding up.
        """
        if not (self.article_agent and getattr(self.article_agent, "ai", None)):
            return None

        used = "; ".join(t["title"] for t in random.sample(
            TOPIC_BANK, min(25, len(TOPIC_BANK))))
        sections = sorted({t["category"] for t in TOPIC_BANK})

        raw = await self.article_agent.ai.generate(
            task="article",
            system_prompt=(
                "You propose explainer topics for a news site. You return one "
                "topic as strict JSON and nothing else."),
            user_prompt=(
                "Propose ONE evergreen explainer topic.\n\n"
                f"Allowed sections: {', '.join(sections)}\n"
                f"Already covered, do not repeat or rephrase: {used}\n\n"
                "It must be a question or how-to that people search for "
                "repeatedly, answerable without breaking news, and still true "
                "in two years. Prefer specific over broad.\n\n"
                'Return exactly: {"category": "...", "title": "...", '
                '"angle": "...", "photo": "..."}\n'
                '"photo" must be one or two CONCRETE nouns that a photograph '
                'could show - "office workspace", not "productivity".'),
            max_tokens=700,
            temperature=0.9,
        )

        topic = self.article_agent._parse_json(raw) if raw else None
        if not topic or not all(topic.get(k) for k in
                                ("category", "title", "angle", "photo")):
            logger.warning("Could not invent a usable evergreen topic.")
            return None

        if topic["category"] not in sections:
            topic["category"] = "world_news"

        if await self._already_written(topic["title"]):
            logger.info(f"Invented topic '{topic['title'][:44]}' is already "
                        f"written; skipping this slot.")
            return None

        logger.info(f"New evergreen topic invented: '{topic['title'][:56]}'")
        return topic

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
