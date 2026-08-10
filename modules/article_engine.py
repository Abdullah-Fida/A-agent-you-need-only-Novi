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
                 site_url: str = ""):
        self.ai = ai_engine
        self.db = db
        self.site_name = site_name
        self.site_url = (site_url or "").rstrip("/")
        self.articles_written = 0
        logger.info("ArticleAgent initialized.")

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

    async def generate_and_publish_article(self, story: Dict,
                                           main_image_url: str = "") -> Optional[Dict]:
        """
        Full pipeline: write → SEO metadata → unique slug → save to Supabase.
        Returns the saved article record, or None on failure.
        """
        title = (story.get("title") or "").strip() or "Breaking News Update"
        summary = (story.get("summary") or "").strip()
        category = story.get("category") or "News"
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

        seo = await self._write_seo(title, summary, body_html, category)

        base_slug = self._slugify(seo.get("slug_hint") or title)
        slug = await self._unique_slug(base_slug)

        record = {
            "title": title[:300],
            "slug": slug,
            "content": body_html,
            "summary": (seo.get("summary") or summary or "")[:600],
            "main_image_url": main_image_url or "",
            "category": category,
            "seo_keywords": seo.get("keywords", [])[:12],
            "meta_title": (seo.get("meta_title") or title)[:70],
            "meta_description": (seo.get("meta_description") or summary or "")[:160],
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
        system_prompt = (
            "You output SEO metadata as a single JSON object and nothing else.\n"
            "Begin your reply with { and end it with }. No explanation, no "
            "reasoning, no code fences.\n"
            "Keys: meta_title, meta_description, keywords (array of lowercase "
            "terms), summary, slug_hint (short lowercase phrase)."
        )
        user_prompt = (
            f"HEADLINE: {title}\nCATEGORY: {category}\n\nARTICLE:\n{excerpt}\n\n"
            f"Respond with the JSON object only."
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
            return {
                "meta_title": str(parsed.get("meta_title") or title)[:70],
                "meta_description": str(parsed.get("meta_description") or summary)[:160],
                "keywords": [str(k).lower().strip() for k in keywords if str(k).strip()],
                "summary": str(parsed.get("summary") or summary)[:300],
                "slug_hint": str(parsed.get("slug_hint") or title),
            }

        # Fallback — never block publishing because SEO generation failed
        logger.info("SEO model output unusable; using derived metadata.")
        return {
            "meta_title": title[:70],
            "meta_description": (summary or self._strip_html(body_html))[:160],
            "keywords": self._derive_keywords(f"{title} {summary}", category),
            "summary": (summary or self._strip_html(body_html))[:300],
            "slug_hint": title,
        }

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

    @staticmethod
    def _derive_keywords(text: str, category: str) -> List[str]:
        words = re.findall(r"[a-z]{4,}", (text or "").lower())
        freq: Dict[str, int] = {}
        for w in words:
            if w not in STOPWORDS:
                freq[w] = freq.get(w, 0) + 1
        top = sorted(freq, key=freq.get, reverse=True)[:8]
        return list(dict.fromkeys([category.lower().replace("_", " ")] + top))
