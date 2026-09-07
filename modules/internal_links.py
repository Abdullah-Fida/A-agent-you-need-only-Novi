"""
Contextual internal links, inserted after the article is written.

An audit of the first 84 published articles found ZERO links between them.
Every piece was an island. That costs twice over: Google reads a site's
internal linking as the map of what it covers deeply, and a reader who
finishes an article with nowhere to go leaves.

WHY THIS IS NOT DONE IN THE PROMPT.
The obvious approach is to hand the model a list of published slugs and ask
it to link them. It invents URLs -- a model given ten real slugs will cheerfully
produce an eleventh that reads plausibly and 404s. Links have to be built from
records that are known to exist, so they are inserted here, afterwards, from
rows read out of the database.

The anchor text is always a phrase already in the sentence. That is what makes
it descriptive rather than "click here", and it means the link cannot change
what the sentence says -- it only makes part of it clickable.
"""
import logging
import re
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger("OmniBot.InternalLinks")

# Enough to build a cluster, few enough that the paragraph still reads as
# prose. Six links in eight hundred words looks like a link farm.
MAX_LINKS = 4

# One link per paragraph. Two in the same paragraph read as decoration.
MAX_PER_PARAGRAPH = 1

# Shorter than this and the anchor is not descriptive: "the bank" tells a
# reader nothing about where they are going. Ten rather than fourteen --
# fourteen threw away "Bitcoin ETFs" (12 characters), which is both specific
# and exactly what a reader would click.
MIN_ANCHOR_CHARS = 10
MIN_ANCHOR_WORDS = 2

# A single word can be an anchor when it is distinctive enough to stand on
# its own -- "Sberbank", "remittances", "Ethereum". Requiring two words
# meant proper nouns, the most descriptive anchors available, were the one
# thing that could never be linked.
MIN_SOLO_CHARS = 8

# Words that cannot carry an anchor on their own.
_WEAK = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for",
    "with", "at", "by", "from", "as", "is", "are", "was", "were", "be",
    "this", "that", "it", "its", "has", "have", "had", "will", "would",
    "new", "how", "why", "what", "when", "who", "more", "than", "after",
    "over", "into", "about", "could", "should", "may", "can",
}

# Long enough to pass the solo test, far too vague to describe an article.
# Every one of these produced a real bad link in testing: "sentiment" sent
# readers to a European stocks story, "credibility" to a bitcoin market
# piece, "services" to a UAE exchange launch.
_VAGUE = {
    "sentiment", "credibility", "services", "economic", "economy",
    "financial", "industry", "business", "markets", "market", "investors",
    "companies", "company", "customers", "products", "platform", "platforms",
    "technology", "development", "developments", "announcement", "strategy",
    "operations", "activity", "concerns", "confidence", "uncertainty",
    "conditions", "performance", "potential", "position", "response",
    "situation", "approach", "interest", "increase", "decrease", "billion",
    "million", "percent", "national", "international", "government",
    "authorities", "officials", "analysts", "reported", "according",

    # WORDS WITH TWO MEANINGS, which is what makes them dangerous as a
    # one-word anchor. Each of these shipped a wrong link on the live site:
    #   "contract"  -> a story about Lebanon's economy CONTRACTING
    #   "deposits"  -> an XRP bridge drained by a software mistake
    #   "recovery"  -> a bitcoin bounty AND a Nepal flood, at the same time
    #   "struggle"  -> a Brazilian general's memoir
    # The word was genuinely in the target's title; it just meant something
    # else there. A reader clicking "contract" expects a contract.
    "contract", "contracts", "deposit", "deposits", "recovery", "struggle",
    "distress", "analysis", "nations", "transfer", "transfers", "growth",
    "decline", "surge", "launch", "release", "support", "pressure",
    "breach", "review", "plans", "measures", "efforts", "talks", "deal",
    "deals", "returns", "issues", "results", "figures", "changes", "moves",
}

# Only body prose is linkable. Never a heading -- a link in an <h2> looks
# like a navigation error -- and never a blockquote, which is someone else's
# words and must not be edited.
_BLOCK = re.compile(r"<(p|li)\b[^>]*>(.*?)</\1>", re.S | re.I)


def _words(text: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9']+", (text or "").lower())


def _trim(parts: List[str]) -> str:
    """
    Drops function words from both ends of a candidate anchor.

    A window slid across a headline lands on "of Ethereum" and "Ethereum
    and" as often as on "Ethereum supply". The words are right; the edges
    are wrong, and a link that begins with "of" reads like a mistake.
    """
    words = list(parts)
    while words and _words(words[0]) and _words(words[0])[0] in _WEAK:
        words.pop(0)
    while words and _words(words[-1]) and _words(words[-1])[-1] in _WEAK:
        words.pop()
    return " ".join(words)


def _significant(phrase: str, solo_ok: bool = False) -> bool:
    parts = _words(phrase)
    if not parts:
        return False
    if len(parts) == 1:
        # Only where the caller vouches that the word describes the whole
        # article -- and even then it has to be long enough to be specific,
        # and not one of the vague abstractions a headline is full of.
        return (solo_ok and len(parts[0]) >= MIN_SOLO_CHARS
                and parts[0] not in _WEAK and parts[0] not in _VAGUE)
    if len(parts) < MIN_ANCHOR_WORDS or len(phrase) < MIN_ANCHOR_CHARS:
        return False
    # At least one word carrying meaning, so "of the year" cannot be an anchor.
    return any(w not in _WEAK and len(w) > 3 for w in parts)


def anchor_phrases(article: Dict) -> List[str]:
    """
    Phrases that would honestly describe this article if clicked.

    Its own keywords first -- they were chosen to describe it -- then runs
    of words from its title. Longest first, so a link lands on the most
    specific phrase available rather than the first two words that match.
    """
    phrases: List[str] = []

    # Keywords describe the WHOLE article, so a single one is allowed to
    # carry a link on its own.
    keyword_words: Set[str] = set()
    for kw in (article.get("seo_keywords") or []):
        kw = (kw or "").strip()
        keyword_words.update(_words(kw))
        if _significant(kw, solo_ok=True):
            phrases.append(kw)

    # Title runs must be at least two words. A single word lifted out of a
    # headline is not what the article is ABOUT, it is merely a word the
    # headline contains -- which produced "Ethereum" pointing at a story
    # about Sberbank, and "phishing" pointing at an app launch delay. A
    # misleading link is worse than a missing one.
    title = re.sub(r"[^\w\s'-]", " ", article.get("title") or "")
    parts = title.split()
    for size in (5, 4, 3, 2):
        for i in range(len(parts) - size + 1):
            run = _trim(parts[i:i + size])
            if not run:
                continue
            # Trimming "of Ethereum" leaves one word. That is allowed to
            # stand alone only when the article's own keywords confirm the
            # piece is ABOUT it -- which is the difference between linking
            # "Ethereum" to an Ethereum story and to a Sberbank one.
            solo = len(_words(run)) == 1 and _words(run)[0] in keyword_words
            if _significant(run, solo_ok=solo):
                phrases.append(run)

    seen: Set[str] = set()
    unique = []
    for p in sorted(phrases, key=len, reverse=True):
        key = " ".join(_words(p))
        if key and key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


class LinkBudget:
    """
    Caps how often any one article can be linked to, and how often with the
    same words.

    Linking a whole archive at once concentrates badly if nothing stops it.
    A dry run over 121 articles produced 24 links reading "cryptocurrency"
    all pointing at one page, 18 reading "blockchain" at another, and 15
    reading "inflation" at a third. Identical anchor text repeated at that
    scale is the classic exact-match over-optimisation pattern, and it is
    read as manipulation rather than helpfulness.

    Seed `inbound` with what the site already has, so a backfill adds to
    those counts instead of starting from zero.
    """

    # Enough to signal that a page matters, far short of a link scheme.
    MAX_INBOUND = 5

    # The same phrase pointing at the same page, more than twice, is a
    # pattern rather than a coincidence.
    MAX_SAME_ANCHOR = 2

    def __init__(self, inbound: Optional[Dict[str, int]] = None):
        from collections import Counter, defaultdict
        self.inbound = Counter(inbound or {})
        self.anchors = defaultdict(Counter)

    def allows(self, slug: str, anchor: str) -> bool:
        if self.inbound[slug] >= self.MAX_INBOUND:
            return False
        return self.anchors[slug][anchor.lower()] < self.MAX_SAME_ANCHOR

    def record(self, slug: str, anchor: str) -> None:
        self.inbound[slug] += 1
        self.anchors[slug][anchor.lower()] += 1


class InternalLinker:
    """Finds published articles worth linking, and links them."""

    def __init__(self, db=None):
        self.db = db
        self.linked = 0
        self.last_error = ""
        # Site-wide link counts, refreshed periodically. See _site_budget.
        self._budget: Optional["LinkBudget"] = None
        self._budget_expires = 0.0

    async def candidates(self, category: str, exclude_slug: str = "",
                         limit: int = 80) -> List[Dict]:
        """
        Published articles this one could point at.

        Same section first: that is what builds a topical cluster rather
        than a random web of links across unrelated sections.
        """
        if not self.db:
            return []
        try:
            rows = await self.db.recent_articles(limit=limit)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.warning(f"Could not read link candidates: {self.last_error}")
            return []

        rows = [r for r in (rows or []) if r.get("slug") and r["slug"] != exclude_slug]
        same = [r for r in rows if (r.get("category") or "") == category]
        other = [r for r in rows if (r.get("category") or "") != category]
        return same + other

    # ── insertion ────────────────────────────────────────────────

    @staticmethod
    def _linkable_spans(block: str) -> List[Tuple[int, int]]:
        """
        Regions of a block that are plain text: outside every tag, and
        outside any anchor that is already there.
        """
        spans, depth, cursor = [], 0, 0
        for m in re.finditer(r"<(/?)(a)\b[^>]*>|<[^>]+>", block, re.I):
            if cursor < m.start() and depth == 0:
                spans.append((cursor, m.start()))
            if m.group(2):
                depth += -1 if m.group(1) else 1
                depth = max(0, depth)
            cursor = m.end()
        if cursor < len(block) and depth == 0:
            spans.append((cursor, len(block)))
        return spans

    @classmethod
    def _find(cls, block: str, phrase: str) -> Optional[Tuple[int, int]]:
        """Where `phrase` appears as plain text, or None."""
        pattern = re.compile(
            r"\b" + r"\s+".join(re.escape(w) for w in phrase.split()) + r"\b", re.I)
        for start, end in cls._linkable_spans(block):
            m = pattern.search(block, start, end)
            if m:
                return m.span()
        return None

    def insert(self, html: str, articles: List[Dict],
               max_links: int = MAX_LINKS,
               budget: Optional["LinkBudget"] = None) -> Tuple[str, List[str]]:
        """
        Returns (html, slugs linked).

        Never rewrites a sentence: it wraps a phrase that is already there.
        If no candidate phrase appears in the text, the article simply gets
        fewer links -- a forced link on a phrase that does not fit is worse
        than no link at all.
        """
        if not (html and articles):
            return html, []

        blocks = list(_BLOCK.finditer(html))
        if not blocks:
            return html, []

        used_targets: Set[str] = set()
        per_block: Dict[int, int] = {}
        # (block index, start, end, slug) collected before anything is
        # rewritten, so the offsets stay valid while they are being chosen.
        edits: List[Tuple[int, int, int, str]] = []

        for article in articles:
            if len(edits) >= max_links:
                break
            slug = article.get("slug")
            if not slug or slug in used_targets:
                continue

            placed = False
            for phrase in anchor_phrases(article):
                if placed:
                    break
                for bi, block in enumerate(blocks):
                    if per_block.get(bi, 0) >= MAX_PER_PARAGRAPH:
                        continue
                    body = block.group(2)
                    found = self._find(body, phrase)
                    if not found:
                        continue
                    # Offsets relative to the whole document.
                    base = block.start(2)
                    start, end = base + found[0], base + found[1]
                    if any(not (end <= s or start >= e) for _, s, e, _ in edits):
                        continue
                    # Site-wide caps: no page may collect too many
                    # inbound links, and the same phrase may not point at
                    # the same page more than twice.
                    anchor = body[found[0]:found[1]]
                    if budget is not None and not budget.allows(slug, anchor):
                        continue
                    edits.append((bi, start, end, slug))
                    per_block[bi] = per_block.get(bi, 0) + 1
                    used_targets.add(slug)
                    if budget is not None:
                        budget.record(slug, anchor)
                    placed = True
                    break

        if not edits:
            return html, []

        out = html
        for _, start, end, slug in sorted(edits, key=lambda e: -e[1]):
            anchor = out[start:end]
            out = f'{out[:start]}<a href="/{slug}">{anchor}</a>{out[end:]}'

        self.linked += len(edits)
        slugs = [slug for _, _, _, slug in sorted(edits, key=lambda e: e[1])]
        logger.info(f"Inserted {len(edits)} internal link(s): {', '.join(slugs)}")
        return out, slugs

    # How long the site-wide link counts are trusted before being re-read.
    BUDGET_TTL_SECONDS = 6 * 3600

    async def _site_budget(self) -> Optional["LinkBudget"]:
        """
        The site's current inbound-link counts, cached.

        Without this the cap only ever applied to a bulk backfill, and the
        day-to-day path drifted into the same over-optimisation slowly
        instead of all at once: "inflation" appeared in two of three test
        articles pointing at the same page, and at eight articles a day
        that reaches thirty links to one target inside a month.

        Counted from article bodies, which means reading them -- so it is
        done once every six hours rather than per article.
        """
        import time
        now = time.monotonic()
        if self._budget is not None and now < self._budget_expires:
            return self._budget
        if not self.db or not hasattr(self.db, "article_bodies"):
            return None
        try:
            from collections import Counter
            counts: Counter = Counter()
            for row in await self.db.article_bodies(limit=400):
                for slug in re.findall(r'<a href="/([a-z0-9][a-z0-9-]{6,})"',
                                       row.get("content") or ""):
                    counts[slug] += 1
            self._budget = LinkBudget(counts)
            self._budget_expires = now + self.BUDGET_TTL_SECONDS
            logger.info(f"Link budget refreshed: {sum(counts.values())} existing "
                        f"internal links across {len(counts)} targets.")
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.warning(f"Could not build the link budget: {self.last_error}")
            return None
        return self._budget

    async def link(self, html: str, category: str, slug: str = "",
                   max_links: int = MAX_LINKS) -> Tuple[str, List[str]]:
        """Read the candidates and link them. Never raises."""
        try:
            found = await self.candidates(category, exclude_slug=slug)
            budget = await self._site_budget()
            return self.insert(html, found, max_links=max_links, budget=budget)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.warning(f"Internal linking failed: {self.last_error}")
            return html, []
