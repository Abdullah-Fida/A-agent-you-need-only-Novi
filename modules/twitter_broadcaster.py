import logging
import os
import asyncio
from typing import Optional, Dict, Any

logger = logging.getLogger("OmniBot.Broadcaster.Twitter")

# Playwright is imported lazily inside post(). Importing it at module level
# meant a missing/broken playwright install crashed the ENTIRE bot at startup,
# taking down Telegram posting and signal copying with it.
Page = Any
BrowserContext = Any

class TwitterBroadcaster:
    """
    Handles safe and automated posting to Twitter/X using Playwright headless browser.
    Bypasses official API limits by acting like a real user.
    """
    def __init__(self, username: str, password: str, email: str,
                 telegram_channel: str = "@Novi_Network", db=None, brain=None):
        self.username = username
        self.password = password
        self.email = email
        self.telegram_channel = telegram_channel
        self.db = db
        self.brain = brain

        # Store browser state (cookies) so we don't have to log in every time
        self.state_file = os.path.join(os.path.dirname(__file__), "..", "twitter_auth.json")

        # Browser automation spawns a full Chromium instance (~300-400MB RSS).
        # Render's free tier only has 512MB, so enabling this there OOM-kills
        # the entire bot mid-post. It must be opted into explicitly.
        self.enabled = os.environ.get("ENABLE_TWITTER", "").strip().lower() in ("1", "true", "yes")
        self._connected = bool(username and password) and self.enabled

        if not username or not password:
            logger.warning("Twitter credentials missing. Twitter posting disabled.")
        elif not self.enabled:
            logger.warning(
                "Twitter posting is DISABLED. Browser automation needs ~400MB RAM and will "
                "crash a 512MB Render instance. Set ENABLE_TWITTER=true to turn it on."
            )

    async def _login(self, page: Page):
        """Perform the actual login flow on X.com"""
        logger.info("Navigating to Twitter login page...")
        await page.goto("https://x.com/i/flow/login")
        
        # 1. Enter Username
        logger.info("Entering username...")
        await page.wait_for_selector('input[autocomplete="username"]', state="visible", timeout=15000)
        await page.fill('input[autocomplete="username"]', self.username)
        await page.keyboard.press("Enter")
        
        # 2. Check for unusual activity email prompt
        try:
            # Wait a few seconds to see if the "Enter your phone number or email" screen appears
            await page.wait_for_selector('input[data-testid="ocfEnterTextTextInput"]', timeout=3000)
            logger.info("Twitter requested email verification (unusual login detected).")
            await page.fill('input[data-testid="ocfEnterTextTextInput"]', self.email)
            await page.keyboard.press("Enter")
        except Exception:
            # Normal flow, no email verification needed
            pass
            
        # 3. Enter Password
        logger.info("Entering password...")
        await page.wait_for_selector('input[name="password"]', state="visible", timeout=10000)
        await page.fill('input[name="password"]', self.password)
        await page.keyboard.press("Enter")
        
        # 4. Wait for Home Timeline to load (verifies successful login)
        logger.info("Waiting for login to complete...")
        await page.wait_for_selector('[data-testid="primaryColumn"]', timeout=20000)
        logger.info("✅ Successfully logged into Twitter via Browser!")

    async def post(self, content_package: Dict) -> bool:
        """
        Publishes a tweet with optional image using browser automation.

        Wrapped in a hard timeout: a stuck browser (captcha, network hang)
        previously blocked this coroutine indefinitely.
        """
        if not self._connected:
            logger.warning("[OFFLINE] Would have posted to Twitter.")
            return False

        if self.brain and not self.brain.can_post_x():
            return False

        try:
            result = await asyncio.wait_for(self._post_impl(content_package), timeout=240)
            if result and self.brain:
                self.brain.record_x_post()
            return result
        except asyncio.TimeoutError:
            logger.error("Twitter post timed out after 4 minutes. Browser was killed.")
            if self.db:
                await self.db.log_error(
                    module="TwitterBroadcaster",
                    error_type="Timeout",
                    error_message="Browser automation exceeded 240s and was aborted.",
                    auto_resolved=True
                )
            return False
        except Exception as e:
            logger.error(f"Twitter post failed: {type(e).__name__}: {e}")
            return False

    async def _post_impl(self, content_package: Dict) -> bool:
        tweet_text = content_package.get("tweet_text", "")
        image_path = content_package.get("image_path", "")

        if not tweet_text:
            logger.error("No tweet_text provided. Aborting Twitter post.")
            return False

        logger.info("Starting headless browser session...")

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("Playwright is not installed. Run: pip install playwright && playwright install chromium")
            return False

        async with async_playwright() as p:
            # Launch chromium in headless mode. 
            # Note: We use some args to make the bot look slightly more human/less detectable
            browser = await p.chromium.launch(
                headless=True,
                args=['--disable-blink-features=AutomationControlled']
            )
            
            context: BrowserContext
            
            # Load saved session if it exists to skip login
            if os.path.exists(self.state_file):
                logger.info("Loading saved Twitter session...")
                context = await browser.new_context(
                    storage_state=self.state_file,
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            else:
                logger.info("No saved session found. Fresh login required.")
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )

            page = await context.new_page()
            
            try:
                # Go to home page to check if we are logged in
                await page.goto("https://x.com/home", wait_until="networkidle")
                
                # If we get redirected to login, or login buttons are present, do login
                if "login" in page.url or await page.locator('[data-testid="loginButton"]').count() > 0:
                    logger.info("Session expired or missing. Logging in...")
                    await self._login(page)
                    # Save the cookies/session so we don't have to log in next time
                    await context.storage_state(path=self.state_file)
                
                logger.info("Navigating to Tweet composer...")
                await page.goto("https://x.com/compose/tweet", wait_until="networkidle")
                
                # Locate the tweet text area and type the text
                logger.info("Typing tweet...")
                await page.wait_for_selector('[data-testid="tweetTextarea_0"]', timeout=15000)
                await page.click('[data-testid="tweetTextarea_0"]')
                
                # Using page.keyboard.type is safer than fill for simulating human typing on rich text areas
                await page.keyboard.insert_text(tweet_text)
                await page.wait_for_timeout(1000)
                
                # Upload image if provided
                if image_path and os.path.exists(image_path):
                    logger.info(f"Uploading image: {image_path}")
                    # Find the hidden file input element
                    file_input = page.locator('input[type="file"]')
                    await file_input.set_input_files(image_path)
                    # Wait for image to process and preview to appear
                    await page.wait_for_selector('[data-testid="attachments"]', timeout=15000)
                    await page.wait_for_timeout(2000) # Buffer for image processing
                
                # Click Post
                logger.info("Clicking the Post button...")
                await page.click('[data-testid="tweetButton"]')
                
                # Wait for the confirmation toast ("Your Tweet was sent.")
                await page.wait_for_selector('[data-testid="toast"]', timeout=15000)
                logger.info("✅ Successfully posted to Twitter!")
                
                # Log to Supabase
                if self.db:
                    await self.db.log_post(
                        platform="twitter",
                        content=tweet_text,
                        image_path=image_path,
                        status="posted",
                        metadata={"type": "browser_automation"}
                    )
                return True
                
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Failed to post to Twitter using browser: {error_msg}")
                
                # Take a screenshot so the user can see what went wrong (e.g. captcha)
                error_shot = os.path.join(os.path.dirname(__file__), "..", "twitter_error.png")
                await page.screenshot(path=error_shot)
                logger.error(f"Saved error screenshot to {error_shot}")
                
                if self.db:
                    await self.db.log_error(
                        module="TwitterBroadcaster",
                        error_type="PlaywrightError",
                        error_message=error_msg,
                        auto_resolved=False
                    )
                return False
            finally:
                await browser.close()
