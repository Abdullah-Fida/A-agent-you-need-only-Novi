"""
Content Engine Module.
Orchestrates the full pipeline: Scrape news -> AI Synthesis -> Image Generation.
Produces ready-to-publish content packages for each platform.
"""
import logging
import random
from typing import Optional, Dict, List
from modules.news_scraper import NewsScraper
from modules.image_generator import ImageGenerator
from modules.article_engine import ArticleAgent
from core.ai_engine import AIEngine

logger = logging.getLogger("OmniBot.ContentEngine")


class ContentEngine:
    """
    The core content production pipeline.
    Pulls news from RSS feeds, synthesizes with AI using the Pakistani lens,
    generates branded images, and returns a content package ready for broadcasting.
    """
    
    # Content mix ratios: International + Crypto + Pakistani
    CONTENT_MIX = [
        ("tech_ai", 0.25),
        ("business_markets", 0.20),
        ("world_news", 0.20),
        ("crypto", 0.25),
        ("pakistan", 0.10),
    ]
    
    def __init__(self, ai_engine: AIEngine, scraper: NewsScraper, 
                 image_gen: ImageGenerator, db=None):
        self.ai = ai_engine
        self.scraper = scraper
        self.image_gen = image_gen
        self.db = db
        self.article_agent = ArticleAgent(ai_engine=ai_engine, db=db)
        self.posts_generated_today = 0
        logger.info("Content Engine & ArticleAgent initialized.")
    
    def _select_category(self) -> str:
        """
        Selects the next content category based on the defined mix ratios.
        Uses weighted random selection.
        """
        categories = [cat for cat, _ in self.CONTENT_MIX]
        weights = [weight for _, weight in self.CONTENT_MIX]
        return random.choices(categories, weights=weights, k=1)[0]
    
    async def produce_content_package(self, category: str = None, progress_callback = None, force: bool = False) -> Optional[Dict]:
        """
        Orchestrates the entire content creation flow:
        1. Select category (if not provided)
        2. Scrape latest news
        3. Group similar stories and pick top story
        4. Synthesize via AI
        5. Generate AI image
        
        Returns:
            A dict containing:
            - telegram_text: Formatted post for Telegram
            - tweet_text: Short version for Twitter
            - image_path: Path to the generated image
            - category: The content category
            - source_credits: Attribution string
        """
        # 1. Select category
        if not category:
            category = self._select_category()
        
        logger.info(f"Producing content package for category: {category}")
        if progress_callback:
            await progress_callback({"step": "scraping", "message": f"Sub-agent is scanning global sources for '{category}'..."})
        
        # 2. Scrape news
        try:
            articles = await self.scraper.fetch_latest_news(category=category, force=force)
        except Exception as e:
            logger.error(f"Scraping failed: {e}")
            if self.db:
                await self.db.log_error(
                    module="ContentEngine",
                    error_type="ScrapingError",
                    error_message=str(e),
                    auto_resolved=False
                )
            return None
        
        if not articles:
            logger.warning(f"No articles found for category: {category}")
            return None
        
        # 3. Group similar stories
        story_groups = self.scraper.group_similar_stories(articles, max_groups=6)
        
        if not story_groups:
            logger.warning("No story groups formed.")
            return None
        
        # Pick the top story group (highest relevance)
        best_group = story_groups[0]
        
        logger.info(f"Selected story group with {len(best_group)} sources. "
                     f"Lead: '{best_group[0]['title'][:60]}...'")
        
        if progress_callback:
            await progress_callback({"step": "synthesis", "message": f"Found trending topic: {best_group[0]['title'][:40]}... Now synthesizing post."})
        
        # 4. AI Synthesis
        niche_context = (
            f"Category: {category.replace('_', '/')}. "
            f"Target audience: International readers, crypto investors, tech professionals, and Pakistani diaspora. "
            f"For crypto news, focus on market impact and investor insights. "
            f"For Pakistani news, explain the local impact. For international news, explain the global significance."
        )
        
        synthesis_result = await self.ai.synthesize_news(best_group, niche_context)
        
        if not synthesis_result:
            logger.error("AI synthesis failed.")
            if self.db:
                await self.db.log_error(
                    module="ContentEngine",
                    error_type="AISynthesisError",
                    error_message="AI engine returned None for synthesis",
                    auto_resolved=False
                )
            return None
        
        telegram_text = synthesis_result["telegram_text"]
        tweet_text = synthesis_result["tweet_text"]
        reddit_title = synthesis_result.get("reddit_title", "")
        reddit_body = synthesis_result.get("reddit_body", "")
        source_credits = synthesis_result["source_credits"]
        
        logger.info(f"AI synthesis complete. Telegram post: {len(telegram_text)} chars.")
        
        # Use the original article title as the image headline to guarantee high quality and prevent AI hallucinations
        image_headline = best_group[0]["title"]
        # Clean the headline (remove quotes, extra punctuation, limit length)
        image_headline = image_headline.strip('"\'')[:80]
        
        if progress_callback:
            await progress_callback({"step": "image_gen", "message": "Generating cinematic HD AI image..."})
        
        image_path = self.image_gen.generate(
            headline=image_headline,
            category=category,
            source_credit=source_credits
        )
        
        if not image_path:
            logger.warning("Image generation failed. Proceeding without image.")
        
        # 6. Generate Website Article via ArticleAgent & Link to Telegram
        article_record = await self.article_agent.generate_and_publish_article(
            story=best_group[0],
            main_image_url=best_group[0].get("real_image_url", "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3")
        )

        if article_record:
            # We don't have a domain yet, so skip the website link for now.
            # article_link = f"\n\n📖 <b>Read Full Detailed Article:</b>\nhttps://novinetwork.com/{article_record['slug']}"
            # telegram_text += article_link
            pass

        # 7. Build and return content package
        self.posts_generated_today += 1
        
        package = {
            "telegram_text": telegram_text,
            "tweet_text": tweet_text,
            "reddit_title": reddit_title,
            "reddit_body": reddit_body,
            "image_path": image_path,
            "category": category,
            "source_credits": source_credits,
            "original_title": best_group[0]["title"],
            "source_count": len(best_group),
            "real_image_url": best_group[0].get("real_image_url", "")
        }
        
        logger.info(f"Content package #{self.posts_generated_today} produced successfully with Website Article link!")
        
        if progress_callback:
            await progress_callback({"step": "complete", "message": "Post is ready!"})
            
        return package
    
    async def produce_morning_brief(self) -> Optional[Dict]:
        """
        Special routine for the morning digest.
        Fetches top stories across ALL categories and creates a combined brief.
        """
        logger.info("Producing morning brief...")
        
        all_articles = await self.scraper.fetch_latest_news(category="all")
        
        if not all_articles:
            return None
        
        # Take the top 3-5 stories by relevance
        top_stories = all_articles[:5]
        
        stories_text = ""
        for i, article in enumerate(top_stories, 1):
            stories_text += f"\n{i}. [{article['source']}] {article['title']}\n"
            stories_text += f"   {article['summary'][:150]}\n"
        
        system_prompt = """You are the editor of "Daily Pulse PK" morning brief.
Write a quick morning digest covering the top 3-5 stories.
Format: Start with "Good morning! Here is your Daily Pulse:" followed by a numbered list.
Each item: 1-2 lines max, with Pakistani relevance.
90% English, 10% Urdu flavor. Use animated emojis like 🔥, 🚀, 📈, 💡, ⚡, 🚨 to make it visual!
End with "Have a productive day! — Daily Pulse PK"
"""
        
        user_prompt = f"Write the morning brief from these top stories:\n{stories_text}"
        
        brief_text = await self.ai.generate(
            task="synthesizer",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=800,
            temperature=0.7
        )
        
        if not brief_text:
            return None
        
        # Generate a morning brief image
        image_path = self.image_gen.generate(
            headline="Morning Brief",
            category="default",
            source_credit="Daily Pulse PK"
        )
        
        return {
            "telegram_text": brief_text,
            "tweet_text": "Your morning brief is live on our Telegram! Top stories that matter for Pakistan today.",
            "image_path": image_path,
            "category": "morning_brief",
            "source_credits": "Multiple sources",
            "original_title": "Morning Brief",
            "source_count": len(top_stories),
        }
