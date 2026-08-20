"""
Article Agent — the auto-blogging engine.

Takes a news story and produces a complete, SEO-ready article for the
website: long-form HTML body, meta title/description, keywords, slug,
reading time, and a hero image.

Model routing: uses the AIEngine "article" task. Swapping in a dedicated
article-writing API later means changing only `AIEngine.MODELS["article"]`
(or passing `model_override`), not this file.
"""
import asyncio
import logging
import re
from typing import Optional, Dict, List

from core.ai_engine import AIEngine

logger = logging.getLogger("OmniBot.ArticleAgent")

# Words stripped from slugs — they add length without SEO value
STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "for",
    "with", "at", "by", "from", "as", "is", "are", "was", "were", "be",
    "this", "that", "it", "its", "has", "have", "had", "will", "would",
}


class ArticleAgent:
    """Generates full SEO articles and publishes them to the website."""

    MIN_ACCEPTABLE_WORDS = 250

    def __init__(self, ai_engine: AIEngine, db=None, site_name: str = "Novi News",
                 site_url: str = "", image_gen=None):
        self.ai = ai_engine
        self.db = db
        self.site_name = site_name
        self.site_url = (site_url or "").rstrip("/")
        self.image_gen = image_gen
        self.articles_written = 0
        logger.info(f"ArticleAgent initialized "
                    f"(image generation: {'on' if image_gen else 'OFF — no generator supplied'}).")

    # ── slug / text helpers ──────────────────────────────────────

    def _slugify(self, text: str, max_len: int = 70) -> str:
        """URL-safe, SEO-friendly slug with stopwords removed."""
        text = (text or "").lower()
        text = re.sub(r"[''`]", "", text)
        text = re.sub(r"[^a-z0-9\s-]", " ", text)
        words = [w for w in text.split() if w and w not in STOPWORDS]
        if not words:
            words = ["news"]

        slug = ""
        for w in words:
            candidate = f"{slug}-{w}" if slug else w
            if len(candidate) > max_len:
                break
            slug = candidate
        return slug.strip("-") or "news"

    async def _unique_slug(self, base: str) -> str:
        """Appends -2, -3 … if the slug is already taken."""
        if not self.db:
            return base
        slug, n = base, 1
        while await self.db.slug_exists(slug):
            n += 1
            slug = f"{base}-{n}"
            if n > 50:
                break
        return slug

    @staticmethod
    def _strip_html(html: str) -> str:
        return re.sub(r"<[^>]+>", " ", html or "")

    def _word_count(self, html: str) -> int:
        return len([w for w in self._strip_html(html).split() if w])

    @staticmethod
    def _clean_html(raw: str) -> str:
        """Removes code fences and stray document wrappers the model may emit."""
        if not raw:
            return ""
        text = raw.strip()
        text = re.sub(r"^```(?:html)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
        text = re.sub(r"</?(?:html|body|head|!DOCTYPE)[^>]*>", "", text, flags=re.I)
        return text.strip()

    # ── generation ───────────────────────────────────────────────

    # Internal pipeline keys -> the section name readers and Google see.
    _DISPLAY_CATEGORIES = {
        "tech_ai": "Tech",
        "tech": "Tech",
        "business_markets": "Business",
        "business": "Business",
        "world_news": "World",
        "crypto": "Crypto",
        "pakistan": "Pakistan",
        "politics": "Politics",
        "sports": "Sport",
    }

    async def generate_and_publish_article(self, story: Dict,
                                           main_image_url: str = "",
                                           category: str = "") -> Optional[Dict]:
        """
        Full pipeline: write → SEO metadata → unique slug → save to Supabase.
        Returns the saved article record, or None on failure.

        `category` is the section the content engine actually picked. Without
        it the story's own field is used, which is often the generic "News" —
        that filed every article under one section and picked the world-news
        illustration for crypto stories.
        """
        title = (story.get("title") or "").strip() or "Breaking News Update"
        summary = (story.get("summary") or "").strip()
        pipeline_category = (category or story.get("category") or "").strip()
        category = self._DISPLAY_CATEGORIES.get(
            pipeline_category.lower().replace(" ", "_"),
            pipeline_category.title() if pipeline_category else "News")
        source_name = story.get("source") or ""
        source_url = story.get("link") or ""

        logger.info(f"ArticleAgent writing: '{title[:60]}'")

        body_html = await self._write_body(title, summary, category)
        if not body_html:
            logger.warning(f"No article body generated for '{title[:50]}'. Skipping.")
            return None

        words = self._word_count(body_html)
        if words < self.MIN_ACCEPTABLE_WORDS:
            logger.warning(f"Article too short ({words} words) for '{title[:50]}'. Skipping.")
            return None

        # SEO metadata and the hero image are independent of each other, so
        # they run together rather than adding their latencies up.
        seo, hero_url = await asyncio.gather(
            self._write_seo(title, summary, body_html, category),
            # The pipeline key drives the illustration, because that is what
            # the image palettes are keyed on.
            self._hero_image(title, pipeline_category or category, main_image_url,
                             story_image_url=story.get("real_image_url", "")),
        )

        base_slug = self._slugify(seo.get("slug_hint") or title)
        slug = await self._unique_slug(base_slug)

        record = {
            "title": title[:300],
            "slug": slug,
            "content": body_html,
            "summary": (seo.get("summary") or summary or "")[:600],
            "main_image_url": hero_url,
            "category": category,
            "seo_keywords": seo.get("keywords", [])[:12],
            "meta_title": self._trim_to_sentence(seo.get("meta_title") or title, 70),
            "meta_description": self._trim_to_sentence(
                seo.get("meta_description") or summary or "", 160),
            "reading_minutes": max(1, round(words / 220)),
            "word_count": words,
            "source_url": source_url,
            "source_name": source_name,
            "author": f"{self.site_name} AI Desk",
            "status": "published",
        }

        saved = None
        if self.db:
            saved = await self.db.save_article(record)
            if not saved:
                logger.error(f"Article '{slug}' could not be saved. "
                             f"Is the 'articles' table present? Run database/schema.sql.")
                return None

        self.articles_written += 1
        logger.info(f"Article published: /{slug} ({words} words, "
                    f"{record['reading_minutes']} min read)")
        return saved or record

    async def _hero_image(self, title: str, category: str,
                          provided_url: str = "", story_image_url: str = "") -> str:
        """
        Returns a publicly reachable hero image URL for the article.

        `provided_url` is normally the picture already generated and uploaded
        for the Telegram post, so the usual path costs nothing: one image is
        made per story and every channel shares it.

        Only when there is no such image does the agent generate its own, and
        only when that fails does it fall back to the photo published with the
        original story.
        """
        if provided_url:
            return provided_url

        if self.image_gen:
            try:
                local_path = await self.image_gen.generate(
                    headline=title,
                    category=self._image_category(category),
                    source_credit=self.site_name,
                    story_image_url=story_image_url,
                )
            except Exception as e:
                logger.error(f"Hero image generation failed: {type(e).__name__}: {e}")
                local_path = None

            if local_path and self.db:
                url = await self.db.upload_image(local_path)
                if url:
                    return url
                logger.warning("Hero image could not be hosted — run database/schema.sql "
                               "to create the 'article-images' bucket.")
        else:
            logger.warning("ArticleAgent has no image generator.")

        # Last resort: the photo the outlet published with the story.
        if story_image_url:
            logger.info("Using the original news photo as the article hero.")
            return story_image_url

        return ""

    @staticmethod
    def _image_category(category: str) -> str:
        """Maps a website category onto an image-generator palette key."""
        key = (category or "").strip().lower().replace(" ", "_")
        aliases = {
            "world": "world_news",
            "news": "world_news",
            "technology": "tech",
            "ai": "tech_ai",
            "markets": "business_markets",
            "finance": "business_markets",
            "economy": "business",
        }
        return aliases.get(key, key)

    async def _write_body(self, title: str, summary: str, category: str) -> Optional[str]:
        """Generates the long-form HTML body."""
        system_prompt = (
            f"You are a senior journalist writing for {self.site_name}, covering "
            f"international news, crypto, technology, business and South Asia.\n\n"
            "Output rules:\n"
            "- Return ONLY clean HTML fragments: <h2>, <h3>, <p>, <ul>, <li>, <strong>, <blockquote>.\n"
            "- NEVER output <html>, <body>, <head>, markdown, or code fences.\n"
            "- Do not repeat the headline as an <h1> — the page renders it separately.\n"
            "- Write in flawless professional English."
        )

        user_prompt = f"""Write a complete 800-1200 word news article.

HEADLINE: {title}
CONTEXT: {summary}
CATEGORY: {category}

Structure it as:
1. A strong opening paragraph that states what happened and why it matters.
2. At least three <h2> sections with substantive analysis, background and context.
3. A short bulleted <ul> of the key takeaways.
4. A forward-looking closing paragraph.

Be factual and analytical. Do not invent specific statistics, quotes or
names that were not provided. Where detail is unknown, write about the
broader implications instead."""

        return self._clean_html(await self.ai.generate(
            task="article",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=3000,
            temperature=0.7,
        ) or "")

    async def _write_seo(self, title: str, summary: str,
                         body_html: str, category: str) -> Dict:
        """Generates SEO metadata, with a safe fallback if the model misbehaves."""
        # Keep the excerpt tight: a long excerpt makes the model echo more of
        # it back, and the reply then gets truncated mid-JSON by max_tokens.
        excerpt = self._strip_html(body_html)[:700]

        # Deliberately NO character-count constraints in this prompt. Asking a
        # reasoning model to respect an exact character limit makes it count
        # letters out loud and burn the whole token budget before emitting any
        # JSON. Lengths are enforced by slicing in code instead (see below).
        # Lengths are stated in WORDS, not characters. Asking for an exact
        # character count makes reasoning models count letters aloud and burn
        # the whole token budget before emitting any JSON; word targets are
        # followed reliably and the exact limits are enforced in code below.
        system_prompt = (
            "You write SEO metadata for a news publisher. You output a single "
            "JSON object and nothing else.\n"
            "Begin your reply with { and end it with }. No explanation, no "
            "reasoning, no code fences.\n\n"
            "Keys and what each must contain:\n"
            "- meta_title: 8-12 words. Must name the actual subject of THIS "
            "story, taken from the headline. Never a generic section label.\n"
            "- meta_description: 22-28 words, one or two full sentences, "
            "stating what specifically happened and why it matters.\n"
            "- keywords: 6-8 entries. Each MUST be a two-to-four word phrase "
            "someone would type into Google. Single generic words such as "
            "'news', 'market', 'crypto', 'business', 'update' are forbidden.\n"
            "- summary: 2 sentences describing this specific story.\n"
            "- slug_hint: 4-7 lowercase words naming the specific event."
        )
        user_prompt = (
            f"HEADLINE: {title}\nCATEGORY: {category}\n\nARTICLE:\n{excerpt}\n\n"
            f"Write metadata about THIS story specifically — a reader must be "
            f"able to tell from the title and description what happened, "
            f"without opening the page. Respond with the JSON object only."
        )

        raw = await self.ai.generate(
            task="seo", system_prompt=system_prompt, user_prompt=user_prompt,
            max_tokens=700, temperature=0.3,
        )

        parsed = self._parse_json(raw)
        if parsed:
            keywords = parsed.get("keywords") or []
            if isinstance(keywords, str):
                keywords = [k.strip() for k in keywords.split(",") if k.strip()]
            return self._repair_seo({
                "meta_title": str(parsed.get("meta_title") or title)[:70],
                "meta_description": str(parsed.get("meta_description") or summary)[:160],
                "keywords": [str(k).lower().strip() for k in keywords if str(k).strip()],
                "summary": str(parsed.get("summary") or summary)[:300],
                "slug_hint": str(parsed.get("slug_hint") or title),
            }, title, summary, body_html, category)

        # Fallback — never block publishing because SEO generation failed
        logger.info("SEO model output unusable; using derived metadata.")
        return self._repair_seo({
            "meta_title": title[:70],
            "meta_description": "",
            "keywords": self._derive_keywords(f"{title} {summary}", category),
            "summary": "",
            "slug_hint": title,
        }, title, summary, body_html, category)

    # Words too generic to earn a ranking on their own.
    _WEAK_KEYWORDS = {
        "news", "market", "markets", "crypto", "business", "update", "updates",
        "today", "latest", "world", "tech", "technology", "finance", "report",
        "article", "story", "trends", "information",
    }

    def _repair_seo(self, seo: Dict, title: str, summary: str,
                    body_html: str, category: str) -> Dict:
        """
        Replaces metadata that is too thin to rank.

        Smaller models answer this task with section labels rather than
        headlines — "crypto market news and updates" as a meta title, and a
        44-character description. Google treats that as thin content and it
        describes every article on the site identically, so anything that
        weak is rebuilt from the article's own text.
        """
        plain = re.sub(r"\s+", " ", self._strip_html(body_html)).strip()

        # ── meta_title: must be substantial and about THIS story ──
        mt = (seo.get("meta_title") or "").strip()
        title_words = {w for w in re.findall(r"[a-z]{4,}", title.lower())}
        mt_words = {w for w in re.findall(r"[a-z]{4,}", mt.lower())}
        # A section label ("crypto market news and updates") still shares the
        # topic word with the headline, so presence of *any* shared word is too
        # weak a test. Require it to carry a real share of the headline: a
        # genuine rewrite keeps most of the distinctive words, a label keeps one.
        overlap = (len(title_words & mt_words) / len(title_words)) if title_words else 1.0
        if len(mt) < 30 or overlap < 0.4:
            logger.info(f"SEO meta_title was generic ({overlap:.0%} headline overlap); "
                        f"using the headline instead.")
            mt = title
        # Never cut a title mid-word; Google shows the truncation as-is.
        seo["meta_title"] = self._trim_to_sentence(mt, 70)

        # ── meta_description: aim for 120-160 chars ──
        md = (seo.get("meta_description") or "").strip()
        if len(md) < 90:
            candidate = (summary or "").strip()
            if len(candidate) < 90:
                candidate = plain
            if candidate:
                logger.info(f"SEO meta_description was thin ({len(md)} chars); "
                            f"rebuilding from the article.")
                md = self._trim_to_sentence(candidate, 158)
        # Trim at a word boundary: Google renders the cut as written, and
        # a description ending "underscoring its sensiti" looks broken.
        seo["meta_description"] = self._trim_to_sentence(md, 160)

        # ── keywords: drop bare generic words, top up from the article ──
        kws = [k for k in (seo.get("keywords") or [])
               if k and k.lower() not in self._WEAK_KEYWORDS]
        if len(kws) < 4:
            for extra in self._derive_keywords(f"{title} {summary}", category):
                if extra not in kws and extra not in self._WEAK_KEYWORDS:
                    kws.append(extra)
                if len(kws) >= 6:
                    break
        seo["keywords"] = kws[:12]

        if not (seo.get("summary") or "").strip():
            seo["summary"] = self._trim_to_sentence(summary or plain, 300)
        return seo

    @staticmethod
    def _trim_to_sentence(text: str, limit: int) -> str:
        """Cuts at a sentence boundary where possible, so it doesn't end mid-word."""
        text = re.sub(r"\s+", " ", text or "").strip()
        if len(text) <= limit:
            return text
        cut = text[:limit]
        stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        if stop > limit * 0.55:
            return cut[:stop + 1].strip()
        space = cut.rfind(" ")
        return (cut[:space] if space > 0 else cut).strip().rstrip(",;:") + "…"

    @classmethod
    def _parse_json(cls, raw: Optional[str]) -> Optional[Dict]:
        """
        Parses model JSON, tolerating the three things models actually do:
        code fences, surrounding prose, and truncation at the token limit.
        """
        if not raw:
            return None
        import json

        text = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.I)
        text = re.sub(r"\s*```$", "", text).strip()

        # 1. Straight parse
        try:
            out = json.loads(text)
            return out if isinstance(out, dict) else None
        except Exception:
            pass

        # 2. Largest brace-delimited span. Reasoning models emit their working
        # out first, so take the LAST plausible object, not the first brace —
        # which may belong to a JSON snippet quoted inside that reasoning.
        start = text.find("{")
        if start == -1:
            return None
        candidate = text[start:]
        for m in reversed(list(re.finditer(r"\{", text))):
            try:
                out = json.loads(text[m.start():])
                if isinstance(out, dict) and out:
                    return out
            except Exception:
                continue
        end = candidate.rfind("}")
        if end != -1:
            try:
                out = json.loads(candidate[:end + 1])
                if isinstance(out, dict):
                    return out
            except Exception:
                pass

        # 3. Repair truncation: close any open string/array/object
        repaired = cls._repair_truncated_json(candidate)
        if repaired:
            try:
                out = json.loads(repaired)
                if isinstance(out, dict):
                    logger.info("Recovered SEO metadata from truncated model output.")
                    return out
            except Exception:
                return None
        return None

    @staticmethod
    def _repair_truncated_json(text: str) -> Optional[str]:
        """Closes unterminated strings/arrays/objects in a cut-off JSON blob."""
        in_string, escaped, depth_obj, depth_arr = False, False, 0, 0

        for ch in text:
            if escaped:
                escaped = False
                continue
            if ch == "\\" and in_string:
                escaped = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth_obj += 1
            elif ch == "}":
                depth_obj -= 1
            elif ch == "[":
                depth_arr += 1
            elif ch == "]":
                depth_arr -= 1

        if depth_obj <= 0 and depth_arr <= 0 and not in_string:
            return None  # nothing to repair

        out = text
        # Drop a dangling ", "key": " fragment so we don't emit a half key
        if in_string:
            out += '"'
        out = re.sub(r",\s*$", "", out)
        out = re.sub(r",\s*\"[^\"]*\"\s*:\s*$", "", out)
        out += "]" * max(0, depth_arr)
        out += "}" * max(0, depth_obj)
        return out

    # Words that carry no search intent on their own. Kept deliberately broad,
    # because the fallback previously returned things like "notches", "best"
    # and "since" as keywords, which rank for nothing.
    _KEYWORD_NOISE = {
        "about", "after", "again", "against", "amid", "another", "back", "because",
        "been", "before", "being", "best", "better", "between", "both", "could",
        "current", "does", "down", "during", "each", "early", "else", "even",
        "every", "first", "from", "gets", "going", "have", "here", "high", "hits",
        "into", "just", "keep", "known", "last", "late", "less", "like", "long",
        "look", "made", "make", "many", "more", "most", "much", "must", "near",
        "need", "next", "notch", "notches", "only", "over", "past", "post",
        "puts", "reveals", "россия", "said", "says", "sees", "several", "shows",
        "since", "some", "soon", "still", "such", "take", "takes", "than", "that",
        "their", "them", "then", "there", "these", "they", "this", "those",
        "through", "time", "told", "took", "under", "until", "very", "want",
        "week", "well", "were", "what", "when", "where", "which", "while",
        "will", "with", "within", "would", "year", "your",
    }

    @classmethod
    def _derive_keywords(cls, text: str, category: str) -> List[str]:
        """
        Search phrases for an article, used when the model gives none.

        Two-word phrases, not single words: "bitcoin short squeeze" is
        something a person types into Google, "notches" is not. Phrases are
        taken in the order they appear so the strongest — which in a headline
        come first — lead.
        """
        raw = (text or "").lower()
        tokens = re.findall(r"[a-z][a-z0-9]{2,}", raw)

        def useful(word: str) -> bool:
            return word not in STOPWORDS and word not in cls._KEYWORD_NOISE

        # Non-overlapping pairs. A sliding window over "bitcoin short squeeze"
        # yields "bitcoin short" and "short squeeze" and "squeeze bitcoin",
        # which reads as noise; stepping past a consumed word keeps the
        # phrases distinct.
        phrases: List[str] = []
        i = 0
        while i < len(tokens) - 1:
            first, second = tokens[i], tokens[i + 1]
            if useful(first) and useful(second):
                phrase = f"{first} {second}"
                if phrase not in phrases:
                    phrases.append(phrase)
                i += 2
            else:
                i += 1

        # Single words only fill the gaps, and only content-bearing ones.
        singles = [w for w in dict.fromkeys(tokens) if useful(w) and len(w) > 3]

        section = category.lower().replace("_", " ").strip()
        out = ([f"{section} news"] if section else []) + phrases[:5] + singles[:3]
        return list(dict.fromkeys(out))[:8]

