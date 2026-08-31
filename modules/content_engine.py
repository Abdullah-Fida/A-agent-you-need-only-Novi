"""
Content Engine Module.
Orchestrates the full pipeline: Scrape news -> AI Synthesis -> Image Generation.
Produces ready-to-publish content packages for each platform.
"""
import logging
import random
import re
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

    # The same story is not equally interesting everywhere. Crypto, tech,
    # business and world reporting read the same in London, New York or
    # Karachi; a Pakistani domestic story does not travel, and putting one out
    # at 09:00 New York spends the best slot of the day on the smallest
    # possible audience.
    #
    # So the slot picks the mix. Regional stories go out while the West is
    # asleep and Asia is awake; the US-facing slots carry only categories that
    # travel. The daily volume of Pakistan coverage is unchanged -- roughly
    # one every other day either way -- it simply lands where it is read.
    US_FACING_HOURS_PKT = {16, 18, 20, 22}

    GLOBAL_MIX = [
        ("crypto", 0.30),
        ("tech_ai", 0.28),
        ("business_markets", 0.22),
        ("world_news", 0.20),
    ]

    REGIONAL_MIX = [
        ("pakistan", 0.28),
        ("world_news", 0.24),
        ("tech_ai", 0.18),
        ("crypto", 0.16),
        ("business_markets", 0.14),
    ]
    
    def __init__(self, ai_engine: AIEngine, scraper: NewsScraper,
                 image_gen: ImageGenerator, db=None,
                 site_name: str = "Novi News", site_url: str = "",
                 article_ai: AIEngine = None, indexnow=None, photos=None):
        self.ai = ai_engine
        self.scraper = scraper
        self.image_gen = image_gen
        self.db = db
        self.site_name = site_name
        # The article agent may run on its own provider/key/model
        self.article_agent = ArticleAgent(ai_engine=article_ai or ai_engine, db=db,
                                          site_name=site_name, site_url=site_url,
                                          image_gen=image_gen, indexnow=indexnow,
                                          photos=photos)
        self.posts_generated_today = 0
        logger.info("Content Engine & ArticleAgent initialized.")
    
    def _select_category(self, hour_pkt: Optional[int] = None) -> str:
        """
        Picks the next category, weighted by who is awake to read it.

        Slots that land in US waking hours draw from GLOBAL_MIX, which carries
        no regional coverage. Everything else draws from REGIONAL_MIX, where
        Pakistan is weighted up. Passing no hour keeps the old behaviour, so a
        manual "post now" from the dashboard is unaffected.
        """
        if hour_pkt is None:
            mix = self.CONTENT_MIX
        elif hour_pkt in self.US_FACING_HOURS_PKT:
            mix = self.GLOBAL_MIX
        else:
            mix = self.REGIONAL_MIX

        categories = [cat for cat, _ in mix]
        weights = [weight for _, weight in mix]
        return random.choices(categories, weights=weights, k=1)[0]

    @staticmethod
    def _current_hour_pkt() -> int:
        from datetime import datetime, timezone, timedelta
        return (datetime.now(timezone.utc) + timedelta(hours=5)).hour
    
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
            category = self._select_category(self._current_hour_pkt())
        
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
        
        # Awaited, not called synchronously: this reaches out to image
        # providers, and blocking the loop here stalls the scheduler and the
        # Telegram connection along with it.
        # If the feed carried no artwork, read the article page's og:image —
        # one request, for the single story being published. Without this a
        # photo-less feed goes straight to a drawn card.
        story_image_url = await self.scraper.resolve_story_image(best_group[0])

        image_path = await self.image_gen.generate(
            headline=image_headline,
            category=category,
            source_credit=source_credits,
            # Lets the generator fall back to the outlet's own photo before
            # it drops to a drawn card.
            story_image_url=story_image_url,
        )

        if not image_path:
            logger.error("Image generation failed at every tier including the local "
                         "card — this means the disk write failed.")
            if self.db:
                await self.db.log_error(
                    module="ImageGenerator",
                    error_type="ImageGenerationFailed",
                    error_message=f"No image produced for '{image_headline[:80]}'",
                    auto_resolved=False,
                )

        # Publish it once, here. Telegram uploads the local file, but Facebook
        # and the website both need a URL they can fetch, and generating a
        # second picture for them would waste a Bing call and show a different
        # image for the same story on every platform.
        image_url = ""
        if image_path and self.db:
            image_url = await self.db.upload_image(image_path)
        if not image_url:
            # Better a real news photo on Facebook than no picture at all.
            image_url = best_group[0].get("real_image_url", "")
            if image_url:
                logger.info("Generated image is not hosted; social platforms will use "
                            "the original news photo.")
        
        # 6. Build and return content package.
        # The website article is written by Fanout AFTER the Telegram post
        # succeeds, so a slow article generation never delays the post and a
        # failed post never leaves an orphan article behind.
        self.posts_generated_today += 1
        
        package = {
            "telegram_text": telegram_text,
            "tweet_text": tweet_text,
            "reddit_title": reddit_title,
            "reddit_body": reddit_body,
            "image_path": image_path,
            # Public URL for the platforms that cannot upload a local file
            "image_url": image_url,
            # Which tier produced it (bing / story_image / card), carried
            # through so the post record and dashboard show it.
            "image_source": getattr(self.image_gen, "last_source", ""),
            "category": category,
            "source_credits": source_credits,
            "original_title": best_group[0]["title"],
            "source_count": len(best_group),
            "real_image_url": best_group[0].get("real_image_url", ""),
            # The lead story, carried through so Fanout can write the article
            "story": best_group[0],
        }
        
        logger.info(f"Content package #{self.posts_generated_today} produced successfully with Website Article link!")
        
        if progress_callback:
            await progress_callback({"step": "complete", "message": "Post is ready!"})
            
        return package
    
    # ── morning brief helpers ─────────────────────────────────────────

    @staticmethod
    def _extract_brief(raw: Optional[str]) -> Optional[str]:
        """
        Pulls the actual brief out of a model reply, or returns None.

        Reasoning models narrate before they answer ("We need to produce a
        morning brief with a numbered list…"), and that narration would
        otherwise be published to the channel verbatim. Anchoring on the
        greeting discards it; requiring real numbered items rejects a reply
        that is *only* narration.
        """
        if not raw:
            return None

        text = raw.strip()
        text = re.sub(r"^```[a-z]*\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text).strip()

        # Take the LAST greeting: any earlier one belongs to the model
        # quoting the instructions back to itself.
        matches = list(re.finditer(r"good morning", text, re.I))
        if matches:
            text = text[matches[-1].start():].strip()
        elif re.search(r"we need to|the user wants|let me |i should |first,? i", text, re.I):
            return None      # pure narration, no brief in it at all

        items = re.findall(r"^\s*\d+[\.\)]\s+\S", text, re.M)
        if len(items) < 2:
            return None

        return text

    def _compose_brief(self, stories: List[Dict]) -> str:
        """Builds the brief without the AI, from the stories themselves."""
        icons = ["🔥", "🚀", "📈", "💡", "⚡", "🚨"]
        lines = [f"☀️ <b>Good morning! Here is your {self.site_name} brief:</b>", ""]
        for i, story in enumerate(stories[:5], 1):
            title = (story.get("title") or "").strip().rstrip(".")
            source = story.get("source") or ""
            lines.append(f"{icons[(i - 1) % len(icons)]} <b>{i}.</b> {title}")
            if source:
                lines.append(f"    <i>via {source}</i>")
        lines += ["", f"Have a productive day! — {self.site_name}"]
        return "\n".join(lines)

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
        
        system_prompt = f"""You are the editor of the "{self.site_name}" morning brief.
Write a quick morning digest covering the top 3-5 stories.
Format: Start with "Good morning! Here is your {self.site_name} brief:" followed by a numbered list.
Each item: 1-2 lines max, noting why it matters to readers in Pakistan.
Write strictly in 100% professional, flawless English. Do NOT use Urdu, Roman Urdu
or any transliterated words — English only, every single word.
Use emojis like 🔥, 🚀, 📈, 💡, ⚡, 🚨 to make it visual.
End with "Have a productive day! — {self.site_name}"
"""
        
        user_prompt = (
            f"Write the morning brief from these top stories:\n{stories_text}\n\n"
            f"Reply with the brief itself and nothing else. Do not explain your "
            f"approach or restate these instructions — begin directly with "
            f'"Good morning!".'
        )

        raw = await self.ai.generate(
            task="synthesizer",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            # Generous, because a reasoning model spends part of the budget
            # thinking before it writes anything usable.
            max_tokens=1400,
            temperature=0.7
        )

        brief_text = self._extract_brief(raw)
        if not brief_text:
            # The free router sometimes lands on a reasoning model that emits
            # its working out instead of the answer. The brief is just a
            # formatted list, so compose it directly rather than publishing
            # the model's monologue to the channel.
            logger.warning("Morning brief model output unusable — composing it directly.")
            brief_text = self._compose_brief(top_stories)
            if self.db:
                await self.db.log_error(
                    module="ContentEngine",
                    error_type="MorningBriefUnusable",
                    error_message=f"Model returned prose, not a brief: {(raw or '')[:200]}",
                    auto_resolved=True,
                )

        # Generate a morning brief image
        image_path = await self.image_gen.generate(
            headline="Your Morning Brief",
            category="morning_brief",
            source_credit=self.site_name,
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
