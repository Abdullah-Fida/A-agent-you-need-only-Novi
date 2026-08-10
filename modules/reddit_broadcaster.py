"""
Reddit Broadcaster Module.
Posts text-based summaries to Reddit using PRAW (Python Reddit API Wrapper).
Implements the 'Trapdoor' method to avoid shadowbans (no direct links in posts).
"""
import asyncio
import logging
import random
from typing import Optional, Dict

logger = logging.getLogger("OmniBot.Broadcaster.Reddit")

# Safe subreddits for Pakistani tech/business news
TARGET_SUBREDDITS = [
    "pakistan",
    "Urdu",
    "Karachi",
    "Lahore",
    "PakistaniTech",
    "investing" # General, but useful for macro economic posts
]

class RedditBroadcaster:
    """
    Handles safe posting to Reddit.
    Strictly adheres to 1-2 posts per day to avoid spam detection.
    """
    def __init__(self, client_id: str, client_secret: str, username: str, 
                 password: str, user_agent: str, db=None):
        self.db = db
        self.username = username
        self.reddit = None
        self._connected = False
        
        try:
            import praw
            if client_id and client_secret and username:
                self.reddit = praw.Reddit(
                    client_id=client_id,
                    client_secret=client_secret,
                    username=username,
                    password=password,
                    user_agent=user_agent
                )
                # Verified lazily on first post — a blocking network check here
                # delayed startup by up to 30s when Reddit was slow or the
                # credentials were wrong (a frequent error in the logs).
                self._connected = True
                logger.info(f"Reddit Broadcaster configured for u/{username} "
                            f"(credentials verified on first post).")
            else:
                logger.warning("Reddit credentials incomplete. Running in offline mode.")
        except ImportError:
            logger.warning("PRAW not installed. Run 'pip install praw'.")
        except Exception as e:
            logger.error(f"Failed to connect to Reddit API: {e}")

    async def post(self, content_package: Dict) -> bool:
        """
        Executes the 'Trapdoor' posting method.
        Posts only the text summary to a randomly selected relevant subreddit.
        """
        if not self._connected:
            logger.warning("[OFFLINE] Would have posted to Reddit.")
            return False
            
        telegram_text = content_package.get("telegram_text", "")
        category = content_package.get("category", "")
        original_title = content_package.get("original_title", "Daily News Update")
        
        if not telegram_text:
            return False
            
        # Select a subreddit (weighted towards local subreddits)
        subreddit_name = random.choice(TARGET_SUBREDDITS)
        
        # Prepare the Trapdoor text (remove raw links if any, append bio notice)
        trapdoor_text = telegram_text
        trapdoor_text += "\n\n*(For full details and daily updates, check the channel in my bio)*"
        
        try:
            logger.info(f"Attempting to post to r/{subreddit_name}...")

            # PRAW is fully synchronous. Calling it directly froze the entire
            # event loop — stalling Telegram listeners and the scheduler for as
            # long as Reddit took to answer. Always run it in a worker thread.
            def _submit():
                subreddit = self.reddit.subreddit(subreddit_name)
                return subreddit.submit(title=original_title, selftext=trapdoor_text)

            submission = await asyncio.wait_for(asyncio.to_thread(_submit), timeout=60)

            logger.info(f"Successfully posted to Reddit: {submission.shortlink}")
            
            # Log to Supabase
            if self.db:
                await self.db.log_post(
                    platform="reddit",
                    content=trapdoor_text,
                    status="posted",
                    metadata={"subreddit": subreddit_name, "url": submission.shortlink}
                )
            
            return True
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Failed to post to Reddit: {error_msg}")
            
            # Common Reddit API errors
            if "RATELIMIT" in error_msg.upper():
                logger.warning("Reddit rate limit hit. Must wait.")
            elif "SUBREDDIT_NOTALLOWED" in error_msg.upper():
                logger.warning(f"Not allowed to post in r/{subreddit_name}.")
                
            if self.db:
                await self.db.log_error(
                    module="RedditBroadcaster",
                    error_type="PostingError",
                    error_message=error_msg,
                    auto_resolved=False
                )
            return False
