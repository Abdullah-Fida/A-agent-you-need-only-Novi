"""
Cross-platform fan-out.

One place that decides where a published news package also goes: Reddit,
X/Twitter, Facebook (via Buffer), and the website article.

Previously this logic was inline in the main loop, so the on-demand
"post now" path skipped Reddit/X/Facebook entirely and the two routes
could drift apart. Everything now goes through `distribute()`.

Each destination is isolated: one platform failing never stops the others,
and no failure can propagate back into the Telegram post that already
succeeded.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("OmniBot.Fanout")


class Fanout:
    """Distributes one content package across every secondary platform."""

    # A story that could not be illustrated is worth another look shortly:
    # the outlet's own photo often appears minutes after the feed entry, and
    # the generator recovers. Publishing without a picture is not an option,
    # and throwing the story away over a transient image failure is worse.
    ARTICLE_RETRY_MINUTES = 30
    MAX_ARTICLE_ATTEMPTS = 3

    def __init__(self, brain=None, db=None, growth_engine=None,
                 reddit=None, twitter=None, buffer=None, article_agent=None,
                 notification_manager=None, scraper=None, pick_category=None):
        self.brain = brain
        self.db = db
        self.growth = growth_engine
        self.reddit = reddit
        self.twitter = twitter
        self.buffer = buffer
        self.article_agent = article_agent
        self.nm = notification_manager
        # Used only by the website's own publishing run.
        self.scraper = scraper
        self.pick_category = pick_category

        # Stories waiting for another attempt at an article.
        self._deferred: List[Dict] = []

    # ── the website's own publishing run ─────────────────────────

    async def _already_published(self, source_url: str) -> bool:
        """
        Whether this story already has an article.

        Checked on the source URL rather than the headline: two runs of the
        same feed produce the same link but the agent may reword the title,
        so a title check would let duplicates through.
        """
        if not (self.db and source_url):
            return False
        try:
            client = getattr(self.db, "client", None)
            if client is None:
                return False
            res = await asyncio.to_thread(
                lambda: client.table("articles").select("slug")
                .eq("source_url", source_url).limit(1).execute())
            return bool(res.data)
        except Exception as e:
            # Better to risk a duplicate than to skip a story because the
            # lookup failed.
            logger.warning(f"Duplicate check failed: {type(e).__name__}: {e}")
            return False

    async def _existing_slug(self, source_url: str) -> str:
        """The slug of an already-published article for this story, if any."""
        if not (self.db and source_url):
            return ""
        try:
            client = getattr(self.db, "client", None)
            if client is None:
                return ""
            res = await asyncio.to_thread(
                lambda: client.table("articles").select("slug")
                .eq("source_url", source_url).limit(1).execute())
            return (res.data[0]["slug"] if res.data else "")
        except Exception:
            return ""

    async def publish_scheduled_article(self) -> Optional[Dict]:
        """
        Writes and publishes one article, independent of Telegram.

        This is the website's own run. It does not consult the sleep window or
        the Telegram post limit, because neither has anything to do with a web
        page: the first exists so the Telegram account looks human, the second
        protects that account from looking automated.
        """
        if not (self.article_agent and self.scraper):
            return None
        if self.brain and not getattr(self.brain, "website_module_active", False):
            logger.info("Website module is OFF — skipping the scheduled article.")
            return None

        category = self.pick_category() if self.pick_category else "world_news"
        logger.info(f"Scheduled article run — category '{category}'.")

        try:
            stories = await self.scraper.fetch_latest_news(category=category)
        except Exception as e:
            logger.error(f"Scheduled article: scraping failed: {e}")
            await self._record_error("Fanout.scheduled_article", e)
            return None

        if not stories:
            logger.warning(f"Scheduled article: no stories for '{category}'.")
            return None

        # Walk the ranked list until one has not been written up already.
        for story in stories[:12]:
            link = story.get("link") or ""
            if await self._already_published(link):
                continue

            try:
                article = await self.article_agent.generate_and_publish_article(
                    story=story, category=category)
            except Exception as e:
                logger.error(f"Scheduled article raised {type(e).__name__}: {e}")
                await self._record_error("Fanout.scheduled_article", e)
                return None

            if article:
                logger.info(f"Scheduled article published: /{article.get('slug')}")
                return article

            # Deferred rather than failed -- retry it later instead of burning
            # the slot on a story that is only missing a picture.
            if getattr(self.article_agent, "retry_after_minutes", 0):
                self._defer_article(
                    story, {"image_url": "", "category": category},
                    reason=getattr(self.article_agent, "last_skip_reason", "unknown"),
                    attempts=0)
                return None

        logger.info("Scheduled article: every candidate was already published.")
        return None

    # ── deferred articles ────────────────────────────────────────

    def _defer_article(self, story: Optional[Dict], package: Dict,
                       reason: str, attempts: int) -> None:
        """Queues a story for another attempt, or gives up after the cap."""
        if not story:
            return
        if attempts + 1 >= self.MAX_ARTICLE_ATTEMPTS:
            logger.error(f"Giving up on article for "
                         f"'{(story.get('title') or '')[:50]}' after "
                         f"{self.MAX_ARTICLE_ATTEMPTS} attempts — {reason}")
            return

        due = datetime.now(timezone.utc) + timedelta(minutes=self.ARTICLE_RETRY_MINUTES)
        self._deferred.append({
            "story": story,
            # Only the fields the retry needs, so a whole package is not held
            # in memory for half an hour.
            "image_url": package.get("image_url", ""),
            "category": package.get("category", ""),
            "due": due,
            "attempts": attempts + 1,
            "reason": reason,
        })
        logger.info(f"Article for '{(story.get('title') or '')[:50]}' deferred "
                    f"{self.ARTICLE_RETRY_MINUTES} min "
                    f"(attempt {attempts + 2}/{self.MAX_ARTICLE_ATTEMPTS}) — {reason}")

    @property
    def deferred_count(self) -> int:
        return len(self._deferred)

    async def retry_due_articles(self) -> int:
        """
        Rewrites any deferred story whose delay has elapsed.

        Called from the main loop. Returns how many were published this pass.
        """
        if not self._deferred or not self.article_agent:
            return 0
        if self.brain and not getattr(self.brain, "website_module_active", False):
            return 0

        now = datetime.now(timezone.utc)
        due = [d for d in self._deferred if d["due"] <= now]
        if not due:
            return 0
        self._deferred = [d for d in self._deferred if d["due"] > now]

        published = 0
        for item in due:
            title = (item["story"].get("title") or "")[:50]
            logger.info(f"Retrying deferred article: '{title}' "
                        f"(attempt {item['attempts'] + 1}/{self.MAX_ARTICLE_ATTEMPTS})")
            try:
                article = await self.article_agent.generate_and_publish_article(
                    story=item["story"],
                    main_image_url=item["image_url"],
                    category=item["category"],
                )
            except Exception as e:
                logger.error(f"Deferred article raised {type(e).__name__}: {e}")
                await self._record_error("ArticleAgent.retry", e)
                article = None

            if article:
                published += 1
                logger.info(f"Deferred article published: /{article.get('slug')}")
            elif getattr(self.article_agent, "retry_after_minutes", 0):
                self._defer_article(
                    item["story"],
                    {"image_url": item["image_url"], "category": item["category"]},
                    reason=getattr(self.article_agent, "last_skip_reason", "unknown"),
                    attempts=item["attempts"])
        return published

    async def distribute(self, package: Dict, story: Optional[Dict] = None) -> Dict[str, bool]:
        """
        Sends a package everywhere it should go.

        The website article is NOT written here -- it has its own schedule.
        This only looks one up so the Facebook caption can link to it.

        Returns a per-platform result map.
        """
        results: Dict[str, bool] = {}

        # 1. The article for this story is written on the WEBSITE's schedule,
        # not this one. All that is needed here is its slug, if it happens to
        # exist yet, so the Facebook caption can link to it. Creating one here
        # as well would publish the same story twice.
        article_slug = ""
        if self.db and story:
            article_slug = await self._existing_slug(story.get("link", ""))
            if article_slug:
                package["article_slug"] = article_slug
        results["website"] = bool(article_slug)

        # 2. Everything else, in parallel — each isolated from the others
        tasks = {
            "reddit": self._to_reddit(package),
            "twitter": self._to_twitter(package),
            "facebook": self._to_facebook(package, article_slug),
        }
        gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)

        for name, outcome in zip(tasks.keys(), gathered):
            if isinstance(outcome, Exception):
                logger.error(f"{name} fan-out raised {type(outcome).__name__}: {outcome}")
                await self._record_error(f"Fanout.{name}", outcome)
                results[name] = False
            else:
                results[name] = bool(outcome)

        delivered = [k for k, v in results.items() if v]
        logger.info(f"Fan-out complete. Delivered to: {', '.join(delivered) or 'nothing'}")
        return results

    # ── per-platform, each fully guarded ─────────────────────────

    async def _to_reddit(self, package: Dict) -> bool:
        if not self.reddit:
            return False
        if self.brain and not self.brain.can_post_reddit():
            return False
        try:
            ok = await self.reddit.post(package)
            if ok and self.brain:
                self.brain.record_reddit_post()
            return ok
        except Exception as e:
            logger.error(f"Reddit post failed: {type(e).__name__}: {e}")
            await self._record_error("RedditBroadcaster", e)
            return False

    async def _to_twitter(self, package: Dict) -> bool:
        if not self.twitter:
            return False
        try:
            # TwitterBroadcaster enforces its own daily cap and timeout
            return await self.twitter.post(package)
        except Exception as e:
            logger.error(f"Twitter post failed: {type(e).__name__}: {e}")
            await self._record_error("TwitterBroadcaster", e)
            return False

    async def _to_facebook(self, package: Dict, article_slug: str) -> bool:
        if not self.buffer:
            return False
        try:
            ok = await self.buffer.post(package, article_slug=article_slug)
            if ok and self.growth:
                self.growth.record_action(self.growth.ACTION_POST, {"platform": "facebook"})
            return ok
        except Exception as e:
            logger.error(f"Facebook/Buffer post failed: {type(e).__name__}: {e}")
            await self._record_error("BufferBroadcaster", e)
            return False

    async def _record_error(self, module: str, error: Exception):
        if self.brain:
            try:
                await self.brain.handle_error(module, error, can_auto_fix=True,
                                              fix_action="Skipped this platform; others unaffected")
                return
            except Exception:
                pass
        if self.db:
            await self.db.log_error(module, type(error).__name__, str(error), auto_resolved=True)

    @property
    def status(self) -> Dict[str, Any]:
        return {
            "reddit": bool(self.reddit and getattr(self.reddit, "_connected", False)),
            "twitter": bool(self.twitter and getattr(self.twitter, "_connected", False)),
            "facebook": bool(self.buffer and self.buffer.is_ready),
            "website": bool(self.article_agent),
        }
