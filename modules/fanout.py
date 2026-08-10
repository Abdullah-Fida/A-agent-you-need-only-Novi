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
from typing import Any, Dict, Optional

logger = logging.getLogger("OmniBot.Fanout")


class Fanout:
    """Distributes one content package across every secondary platform."""

    def __init__(self, brain=None, db=None, growth_engine=None,
                 reddit=None, twitter=None, buffer=None, article_agent=None,
                 notification_manager=None):
        self.brain = brain
        self.db = db
        self.growth = growth_engine
        self.reddit = reddit
        self.twitter = twitter
        self.buffer = buffer
        self.article_agent = article_agent
        self.nm = notification_manager

    async def distribute(self, package: Dict, story: Optional[Dict] = None) -> Dict[str, bool]:
        """
        Sends a package everywhere it should go.

        Order matters: the website article is written first so its slug can be
        linked from the Facebook caption.

        Returns a per-platform result map.
        """
        results: Dict[str, bool] = {}

        # 1. Website article (gives us the slug for the social links).
        # Gated by the website module toggle, which is OFF by default — while
        # off the Article Agent does not run at all, so it burns no tokens on
        # its (separate) API key.
        article_slug = ""
        website_on = bool(self.brain and getattr(self.brain, "website_module_active", False))

        if self.article_agent and story and website_on:
            try:
                article = await self.article_agent.generate_and_publish_article(
                    story=story,
                    main_image_url=story.get("real_image_url") or package.get("image_url", ""),
                )
                if article:
                    article_slug = article.get("slug", "")
                    package["article_slug"] = article_slug
                    results["website"] = True
                else:
                    results["website"] = False
            except Exception as e:
                logger.error(f"Article publishing failed: {type(e).__name__}: {e}")
                results["website"] = False
                await self._record_error("ArticleAgent", e)
        elif self.article_agent and story and not website_on:
            logger.info("Website module is OFF — skipping article generation.")
            results["website"] = False

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
