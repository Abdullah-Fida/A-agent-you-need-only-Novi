"""
RSS News Scraper Module.
Fetches breaking news from curated RSS feeds across Tech/AI, Business, and World news.
Groups similar stories together for multi-source synthesis.
"""
import logging
import asyncio
import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

logger = logging.getLogger("OmniBot.Scraper")

# Curated RSS Feed Sources organized by content category
RSS_FEEDS = {
    "tech_ai": [
        {"url": "https://techcrunch.com/feed/", "source": "TechCrunch"},
        {"url": "https://www.theverge.com/rss/index.xml", "source": "The Verge"},
        {"url": "https://feeds.arstechnica.com/arstechnica/technology-lab", "source": "Ars Technica"},
    ],
    "business_markets": [
        {"url": "https://feeds.bbci.co.uk/news/business/rss.xml", "source": "BBC Business"},
        {"url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "source": "CNBC"},
        {"url": "https://feeds.bloomberg.com/markets/news.rss", "source": "Bloomberg"},
    ],
    "world_news": [
        {"url": "https://feeds.bbci.co.uk/news/world/rss.xml", "source": "BBC World"},
        {"url": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "source": "NYT World"},
        {"url": "https://www.aljazeera.com/xml/rss/all.xml", "source": "Al Jazeera"},
        {"url": "https://www.theguardian.com/world/rss", "source": "The Guardian"},
    ],
    "crypto": [
        {"url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "source": "CoinDesk"},
        {"url": "https://cointelegraph.com/rss", "source": "CoinTelegraph"},
        {"url": "https://decrypt.co/feed", "source": "Decrypt"},
        {"url": "https://bitcoinmagazine.com/.rss/full/", "source": "Bitcoin Magazine"},
    ],
    "pakistan": [
        {"url": "https://www.dawn.com/feeds/home", "source": "Dawn"},
        {"url": "https://www.geo.tv/rss/1/0", "source": "Geo News"},
        {"url": "https://tribune.com.pk/feed/home", "source": "Express Tribune"},
    ],
    "sports_news": [
        {"url": "https://www.espncricinfo.com/rss/content/story/feeds/0.xml", "source": "ESPN Cricinfo"},
        {"url": "https://www.skysports.com/rss/12040", "source": "Sky Sports"},
    ],
}

# Keywords that indicate Pakistan/South Asia relevance (for priority boosting)
SOUTH_ASIA_KEYWORDS = [
    "pakistan", "india", "bangladesh", "sri lanka", "south asia", "rupee", "imf",
    "cpec", "china-pakistan", "freelancer", "remittance", "islamic finance",
    "karachi", "lahore", "islamabad", "saarc", "brics", "kashmir",
    "inflation", "interest rate", "federal reserve", "oil price", "energy crisis"
]

# Crypto-specific keywords for relevance scoring
CRYPTO_KEYWORDS = [
    "bitcoin", "btc", "ethereum", "eth", "crypto", "blockchain", "defi",
    "nft", "web3", "solana", "xrp", "altcoin", "stablecoin", "usdt",
    "binance", "coinbase", "sec", "regulation", "mining", "halving",
    "token", "wallet", "exchange", "bull", "bear", "market cap"
]


# Events of genuine international consequence. These are what a reader expects
# a news channel to lead with, and they scored nothing before — a routine
# gadget story could outrank a war or an election purely on keyword count.
GLOBAL_HEADLINE_KEYWORDS = [
    "war", "ceasefire", "invasion", "airstrike", "missile", "troops",
    "election", "elected", "president", "prime minister", "parliament",
    "coup", "protest", "sanctions", "treaty", "summit", "united nations",
    "nato", "g7", "g20", "opec",
    "earthquake", "hurricane", "typhoon", "flood", "wildfire", "tsunami",
    "outbreak", "pandemic", "evacuation", "state of emergency",
    "assassination", "resigns", "impeach", "verdict", "indicted",
    "central bank", "interest rate", "inflation", "recession", "default",
    "oil price", "opec+", "market crash", "record high", "bailout",
    "nuclear", "space launch", "breakthrough",
]

# Words that mark a story as a roundup or filler rather than an event.
LOW_VALUE_TITLE_MARKERS = [
    "what happened in", "here's what", "roundup", "recap", "digest",
    "week in review", "things to know", "what to watch", "live updates",
    "best deals", "deal of the day", "coupon", "discount", "sponsored",
    "opinion", "editorial", "horoscope", "quiz",
]


class NewsScraper:
    """
    Scrapes RSS feeds, deduplicates stories, and groups similar articles
    from multiple sources for cross-referencing before AI synthesis.
    """
    
    def __init__(self, db=None):
        self.db = db
        self.seen_hashes = set()  # In-memory deduplication for this session
        logger.info("News Scraper initialized.")
    
    def _hash_title(self, title: str) -> str:
        """Creates a hash of a normalized title for deduplication."""
        normalized = title.lower().strip()
        # Remove common noise words
        for word in ["the", "a", "an", "is", "are", "was", "were", "has", "have"]:
            normalized = normalized.replace(f" {word} ", " ")
        return hashlib.md5(normalized.encode()).hexdigest()[:12]
    
    def _parse_rss(self, xml_text: str, source_name: str) -> List[Dict]:
        """Parses an RSS XML feed into a list of article dicts."""
        articles = []
        try:
            root = ET.fromstring(xml_text)
            
            # Handle both RSS 2.0 (<item>) and Atom (<entry>) formats
            items = root.findall('.//item') or root.findall('.//{http://www.w3.org/2005/Atom}entry')
            
            for item in items[:10]:  # Only take the 10 most recent per feed
                # RSS 2.0 format
                title_elem = item.find('title')
                desc_elem = item.find('description')
                link_elem = item.find('link')
                pub_date_elem = item.find('pubDate')
                
                # Atom format fallback
                if title_elem is None:
                    title_elem = item.find('{http://www.w3.org/2005/Atom}title')
                if desc_elem is None:
                    desc_elem = item.find('{http://www.w3.org/2005/Atom}summary')
                if link_elem is None:
                    link_elem = item.find('{http://www.w3.org/2005/Atom}link')
                
                title = title_elem.text if title_elem is not None and title_elem.text else ""
                summary = desc_elem.text if desc_elem is not None and desc_elem.text else ""
                
                # Get link (handle Atom's href attribute)
                link = ""
                if link_elem is not None:
                    link = link_elem.text or link_elem.get('href', '')
                
                real_image_url = self._extract_image(item)
                
                if not title:
                    continue
                
                # Strip HTML tags from summary
                import re
                summary = re.sub(r'<[^>]+>', '', summary)[:500]
                
                articles.append({
                    "title": title.strip(),
                    "summary": summary.strip(),
                    "link": link.strip(),
                    "source": source_name,
                    "hash": self._hash_title(title),
                    "real_image_url": real_image_url
                })
                
        except ET.ParseError as e:
            logger.error(f"Failed to parse RSS from {source_name}: {e}")
        
        return articles
    
    # Namespaces publishers actually use for item artwork
    _MRSS = "{http://search.yahoo.com/mrss/}"
    _ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"

    @classmethod
    def _extract_image(cls, item) -> str:
        """
        The article's own photograph, from wherever this feed happens to put it.

        Only <enclosure> and <media:content> used to be checked, so feeds that
        publish artwork any other way — Geo News and Dawn among them — looked
        image-less and their stories fell back to a drawn card.
        """
        # 1. <enclosure type="image/...">
        for enc in item.findall("enclosure"):
            if enc.get("type", "").startswith("image") and enc.get("url"):
                return enc.get("url", "").strip()

        # 2. <media:content> — medium="image", an image type, or a bare url
        for media in item.findall(f".//{cls._MRSS}content"):
            url = (media.get("url") or "").strip()
            if not url:
                continue
            if (media.get("medium") == "image"
                    or media.get("type", "").startswith("image")
                    or cls._looks_like_image(url)):
                return url

        # 3. <media:thumbnail>
        for thumb in item.findall(f".//{cls._MRSS}thumbnail"):
            if thumb.get("url"):
                return thumb.get("url", "").strip()

        # 4. <itunes:image href="...">
        itunes = item.find(f".//{cls._ITUNES}image")
        if itunes is not None and itunes.get("href"):
            return itunes.get("href", "").strip()

        # 5. The first <img> inside the description or full content. This is
        # how most publishers ship artwork, and it was being ignored entirely.
        for tag in ("description", "{http://purl.org/rss/1.0/modules/content/}encoded",
                    "{http://www.w3.org/2005/Atom}summary",
                    "{http://www.w3.org/2005/Atom}content"):
            node = item.find(tag)
            if node is None or not node.text:
                continue
            found = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', node.text, re.I)
            if found:
                url = found.group(1).strip()
                if url.startswith("//"):
                    url = "https:" + url
                if url.startswith("http"):
                    return url
        return ""

    @staticmethod
    def _looks_like_image(url: str) -> bool:
        return bool(re.search(r"\.(jpe?g|png|webp|avif)(\?|$)", url or "", re.I))

    async def resolve_story_image(self, story: Dict) -> str:
        """
        Falls back to the article page's own social preview image.

        Some feeds carry no artwork at all, but essentially every news page
        sets og:image — that is the picture the publisher chose to represent
        the story. Called for the single story being published, so it costs
        one request per post rather than one per scraped headline.
        """
        existing = (story.get("real_image_url") or "").strip()
        if existing:
            return existing

        link = (story.get("link") or "").strip()
        if not link.startswith("http"):
            return ""

        def _fetch() -> str:
            req = Request(link, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
            })
            with urlopen(req, timeout=12) as resp:
                html = resp.read(400_000).decode("utf-8", errors="replace")
            for pattern in (
                r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
            ):
                m = re.search(pattern, html, re.I)
                if m:
                    url = m.group(1).strip()
                    if url.startswith("//"):
                        url = "https:" + url
                    if url.startswith("http"):
                        return url
            return ""

        try:
            url = await asyncio.get_event_loop().run_in_executor(None, _fetch)
            if url:
                logger.info(f"Recovered the publisher's photo from og:image: {url[:80]}")
                story["real_image_url"] = url
            return url
        except Exception as e:
            logger.warning(f"Could not read og:image from {link[:60]}: {type(e).__name__}")
            return ""

    async def _fetch_feed(self, feed_url: str, source_name: str) -> List[Dict]:
        """Fetches and parses a single RSS feed."""
        try:
            req = Request(feed_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DailyPulseBot/1.0"
            })
            
            # Run the blocking I/O in a thread pool
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, lambda: urlopen(req, timeout=15))
            xml_text = response.read().decode('utf-8', errors='replace')
            
            articles = self._parse_rss(xml_text, source_name)
            logger.info(f"Fetched {len(articles)} articles from {source_name}.")
            return articles
            
        except URLError as e:
            logger.warning(f"Network error fetching {source_name}: {e}")
            if self.db:
                await self.db.log_error(
                    module="NewsScraper",
                    error_type="NetworkError",
                    error_message=f"Failed to fetch {source_name}: {e}",
                    auto_resolved=True
                )
            return []
        except Exception as e:
            logger.error(f"Unexpected error fetching {source_name}: {e}")
            return []
    
    def _calculate_relevance_score(self, article: Dict) -> int:
        """
        Scores an article on relevance to the target audience.
        Higher score = more relevant.
        Covers: Pakistan/South Asia, Crypto, and general importance.
        """
        score = 0
        text = (article.get("title", "") + " " + article.get("summary", "")).lower()
        
        for keyword in SOUTH_ASIA_KEYWORDS:
            if keyword in text:
                score += 10  # High boost for direct Pakistan/SA relevance
        
        for keyword in CRYPTO_KEYWORDS:
            if keyword in text:
                score += 8  # Strong boost for crypto relevance
        
        # General importance keywords
        for keyword in ["ai", "artificial intelligence", "layoff", "startup", "funding",
                        "dollar", "stock market", "recession", "breaking", "urgent"]:
            if keyword in text:
                score += 3  # Moderate boost for general interest

        # Major world events. Weighted above the topic keywords so a war,
        # an election or a rate decision leads ahead of a routine tech story.
        for keyword in GLOBAL_HEADLINE_KEYWORDS:
            if keyword in text:
                score += 12

        # Roundups and filler make weak posts and weaker articles: the title
        # names no event, so the synthesised copy has nothing concrete to say.
        title = (article.get("title") or "").lower()
        for marker in LOW_VALUE_TITLE_MARKERS:
            if marker in title:
                score -= 25
                break

        return score
    
    async def fetch_latest_news(self, category: str = "all", force: bool = False) -> List[Dict]:
        """
        Fetches news from all RSS feeds in a category (or all categories).
        Returns articles sorted by relevance score, deduplicated.
        
        Args:
            category: 'tech_ai', 'business_markets', 'world_news', or 'all'
            force: If True, bypasses deduplication to always return articles.
        """
        all_articles = []
        
        if category == "all":
            feeds_to_fetch = [feed for feeds in RSS_FEEDS.values() for feed in feeds]
        elif category in RSS_FEEDS:
            feeds_to_fetch = RSS_FEEDS[category]
        else:
            # Dynamic topic search via Google News RSS
            import urllib.parse
            query = urllib.parse.quote(category)
            logger.info(f"Dynamic topic requested: '{category}'. Fetching via Google News RSS.")
            feeds_to_fetch = [{"url": f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en", "source": f"Google News: {category.title()}"}]
        
        if not feeds_to_fetch:
            logger.warning(f"No feeds configured for category: {category}")
            return []
        
        # Fetch all feeds concurrently
        tasks = [self._fetch_feed(f["url"], f["source"]) for f in feeds_to_fetch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in results:
            if isinstance(result, list):
                all_articles.extend(result)
        
        # Deduplicate
        unique_articles = []
        for article in all_articles:
            if force or article["hash"] not in self.seen_hashes:
                if not force:
                    self.seen_hashes.add(article["hash"])
                article["relevance_score"] = self._calculate_relevance_score(article)
                unique_articles.append(article)
        
        # Sort by relevance score (highest first)
        unique_articles.sort(key=lambda x: x["relevance_score"], reverse=True)
        
        logger.info(f"Total unique articles fetched: {len(unique_articles)} "
                     f"(filtered from {len(all_articles)} total)")
        
        return unique_articles
    
    def group_similar_stories(self, articles: List[Dict], max_groups: int = 6) -> List[List[Dict]]:
        """
        Groups articles that cover the same story from different sources.
        This allows the AI to read 2-3 perspectives before synthesizing.
        
        Returns a list of groups, where each group is a list of 1-3 related articles.
        """
        groups = []
        used_indices = set()
        
        for i, article_a in enumerate(articles):
            if i in used_indices:
                continue
            
            group = [article_a]
            used_indices.add(i)
            
            # Find similar articles by comparing title words
            words_a = set(article_a["title"].lower().split())
            
            for j, article_b in enumerate(articles):
                if j in used_indices or j == i:
                    continue
                
                words_b = set(article_b["title"].lower().split())
                # If more than 40% of words overlap, consider them the same story
                overlap = len(words_a & words_b) / max(len(words_a | words_b), 1)
                
                if overlap > 0.4 and len(group) < 3:
                    group.append(article_b)
                    used_indices.add(j)
            
            groups.append(group)
            
            if len(groups) >= max_groups:
                break
        
        logger.info(f"Grouped articles into {len(groups)} story clusters.")
        return groups
