"""
Article Agent Module.
Subagent under NOVI that takes raw news packages and expands them into 
1,000+ word SEO-optimized HTML articles with dynamic slugs and metadata for the website.
"""
import logging
import re
from typing import Optional, Dict
from core.ai_engine import AIEngine

logger = logging.getLogger("OmniBot.ArticleAgent")


class ArticleAgent:
    """
    Long-form article generation subagent for the website ecosystem.
    """

    def __init__(self, ai_engine: AIEngine, db=None):
        self.ai = ai_engine
        self.db = db
        logger.info("ArticleAgent initialized.")

    def _slugify(self, text: str) -> str:
        """Converts a title into a clean URL slug."""
        text = text.lower()
        text = re.sub(r'[^a-z0-9\s-]', '', text)
        text = re.sub(r'[\s-]+', '-', text).strip('-')
        return text[:80]

    async def generate_and_publish_article(self, story: Dict, main_image_url: str) -> Optional[Dict]:
        """
        Takes raw story dictionary and main image URL, generates a full-length SEO article,
        and saves it to the Supabase database.
        """
        title = story.get("title", "Breaking News Update")
        summary = story.get("summary", "")
        category = story.get("category", "News")

        prompt = f"""
        You are an elite senior investigative journalist for Novi News. 
        Write a comprehensive, 800 to 1,200 word in-depth analytical news article based on this story:

        TITLE: {title}
        SUMMARY: {summary}
        CATEGORY: {category}

        REQUIREMENTS:
        1. Format the response as clean HTML (use <h2>, <p>, <ul>, <li>, <strong> tags ONLY). Do NOT include <html> or <body> wrapper tags.
        2. Provide deep analysis, context, implications for Pakistan, and a forward-looking conclusion.
        3. Break the article into at least 3 distinct sections with clear, catchy <h2> subheadings.
        4. Tone: Authoritative, engaging, objective, and analytical.
        """

        try:
            # Generate full HTML content using Groq/OpenAI via AIEngine
            content_html = await self.ai.call_llm(prompt=prompt, system_prompt="You write high-ranking, SEO-optimized news articles in HTML format.")
            
            slug = self._slugify(title)
            seo_keywords = [k.strip() for k in category.split() + title.split()[:4]]

            article_record = {
                "title": title,
                "slug": slug,
                "content": content_html,
                "summary": summary,
                "main_image_url": main_image_url,
                "category": category,
                "seo_keywords": seo_keywords,
                "author": "NOVI ArticleAgent"
            }

            # Save to Supabase if DB is initialized
            if self.db and getattr(self.db, "_initialized", False):
                try:
                    await self.db.client.table("articles").insert(article_record).execute()
                    logger.info(f"Successfully published article to website: /{slug}")
                except Exception as db_err:
                    logger.error(f"Failed to insert article into Supabase: {db_err}")

            return article_record

        except Exception as e:
            logger.error(f"ArticleAgent failed to generate article for '{title}': {e}")
            return None
