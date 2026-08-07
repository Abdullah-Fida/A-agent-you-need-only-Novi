"""
Novi News — Omni-Channel AI Content & Marketing Bot
Main Orchestrator

This is the central entry point. It initializes all modules,
runs the Brain's scheduling loop, and coordinates content production
and broadcasting throughout the day.
"""
import asyncio
import logging
import sys
import os
import random

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.config import load_config
from core.ai_engine import AIEngine
from core.brain import BotBrain
from database.supabase_db import SupabaseDB
from modules.news_scraper import NewsScraper
from modules.image_generator import ImageGenerator
from modules.content_engine import ContentEngine
from modules.telegram_broadcaster import TelegramBroadcaster
from modules.stealth_marketer import StealthMarketer
from modules.reddit_broadcaster import RedditBroadcaster
from modules.twitter_broadcaster import TwitterBroadcaster
from modules.notification_manager import NotificationManager
import uvicorn
from core.api_server import app as api_app

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("omni_bot.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("OmniBot")


async def main():
    logger.info("=" * 60)
    logger.info("  Novi News — Omni-Channel AI Bot Starting...")
    logger.info("=" * 60)
    
    # 1. Load Configuration
    config = load_config()
    
    # 2. Initialize Supabase Database
    db = SupabaseDB(url=config.supabase_url, key=config.supabase_key)
    await db.initialize()
    
    # 3. Initialize AI Engine (Multi-Key)
    ai_engine = AIEngine(api_keys=config.openrouter_api_keys, db=db)
    
    # 4. Initialize Modules
    scraper = NewsScraper(db=db)
    
    images_dir = os.path.join(os.path.dirname(__file__), "assets", "generated_images")
    channel_name = config.channel_username or "Novi_Network"
    image_gen = ImageGenerator(output_dir=images_dir, channel_name=channel_name, bing_cookie=config.bing_cookie)
    
    content_engine = ContentEngine(
        ai_engine=ai_engine,
        scraper=scraper,
        image_gen=image_gen,
        db=db
    )
    
    # 5. Initialize Notification Manager (must be before Broadcaster & Stealth)
    notification_manager = NotificationManager(
        sender_email=config.email_sender,
        app_password=config.email_app_password,
        receiver_email=config.email_receiver,
        resend_api_key=config.resend_api_key,
        db=db
    )
    
    # Telegram Broadcaster is initialized later (step 9) to include the Brain dependency
    
    # 7. Initialize Reddit Broadcaster
    reddit_broadcaster = RedditBroadcaster(
        client_id=config.reddit_client_id,
        client_secret=config.reddit_client_secret,
        username=config.reddit_username,
        password=config.reddit_password,
        user_agent=config.reddit_user_agent,
        db=db
    )
    
    # 7.5 Initialize Twitter Broadcaster
    twitter_broadcaster = TwitterBroadcaster(
        username=config.twitter_username,
        password=config.twitter_password,
        email=config.twitter_email,
        db=db
    )
    
    # 8. Initialize The Brain
    brain = BotBrain(db=db, weekly_goal=config.weekly_subscriber_goal, notification_manager=notification_manager)
    
    # 8. Initialize Stealth Marketer (Inactive initially, controllable by NOVI)
    stealth_marketer = StealthMarketer(
        api_id=config.telegram_api_id,
        api_hash=config.telegram_api_hash,
        stealth_phone=config.stealth_phone,
        ai_engine=ai_engine,
        brain=brain,
        channel_username=config.channel_username,
        target_groups=config.target_stealth_groups,
        engagement_rate=0.01,  # Start highly restricted
        notification_manager=notification_manager,
        db=db,
        session_string=config.stealth_session_string or config.telegram_session_string
    )
    
    # 9. Connect Telegram Broadcaster & Stealth Marketer
    telegram_connected = False
    broadcaster = TelegramBroadcaster(
        api_id=config.telegram_api_id,
        api_hash=config.telegram_api_hash,
        session_string=config.telegram_session_string,
        channel_username=config.channel_username,
        notification_manager=notification_manager,
        db=db,
        brain=brain
    )
    try:
        await broadcaster.connect()
        if broadcaster.is_ready:
            telegram_connected = True
            
            # Get initial subscriber count
            sub_count = await broadcaster.get_subscriber_count()
            brain.week_start_subscribers = sub_count
            brain.current_subscribers = sub_count
            logger.info(f"Initial subscriber count: {sub_count}")
        
        # Start Stealth Marketer (which uses its own Telethon client in the background)
        await stealth_marketer.connect()
        await stealth_marketer.activate()  # Activate by default based on user request
    except Exception as e:
        logger.error(f"Failed to connect Telegram Broadcaster: {e}")
        await brain.handle_error("TelegramBroadcaster", e)
    
    # 10. Start API Server
    logger.info("Starting Dashboard API server on port 8000...")
    api_app.state.brain = brain
    api_app.state.notification_manager = notification_manager
    api_app.state.stealth_marketer = stealth_marketer
    api_app.state.content_engine = content_engine
    api_app.state.telegram_broadcaster = broadcaster
    api_app.state.config = config  # Pass config for OTP auth endpoints
    
    server_port = int(os.environ.get("PORT", 8000))
    config_uv = uvicorn.Config(app=api_app, host="0.0.0.0", port=server_port, loop="asyncio", log_level="warning")
    server = uvicorn.Server(config_uv)
    api_task = asyncio.create_task(server.serve())

    # 11. Start Keep-Alive Self-Ping (Render Free Tier)
    async def self_ping_loop():
        """Pings the /ping endpoint every 10 minutes to prevent Render from sleeping."""
        import aiohttp
        while True:
            await asyncio.sleep(600)  # 10 minutes
            try:
                async with aiohttp.ClientSession() as session:
                    render_url = os.environ.get("RENDER_EXTERNAL_URL")
                    ping_url = f"{render_url}/ping" if render_url else f"http://localhost:{server_port}/ping"
                    async with session.get(ping_url, timeout=aiohttp.ClientTimeout(total=10)):
                        pass
                logger.debug("Keep-alive self-ping successful.")
            except Exception:
                logger.warning("Keep-alive self-ping failed (server may be starting up).")
    
    asyncio.create_task(self_ping_loop())
    logger.info("Keep-alive self-ping loop started (every 10 minutes).")
    
    # 12. Start Telegram Connection Heartbeat
    async def heartbeat_monitor():
        """Checks Telegram connection every 30 minutes. Emails admin if disconnected."""
        while True:
            await asyncio.sleep(1800)  # 30 minutes
            try:
                # Check main broadcaster
                if telegram_connected and broadcaster.client:
                    try:
                        if broadcaster.client.is_connected():
                            logger.info("Heartbeat: Telegram Broadcaster connected ✓")
                        else:
                            logger.error("Heartbeat: Telegram Broadcaster DISCONNECTED!")
                            await notification_manager.notify_connection_status(
                                "Telegram Broadcaster", False, "Connection lost."
                            )
                    except Exception as e:
                        logger.error(f"Heartbeat: Telegram Broadcaster DISCONNECTED! {e}")
                        await notification_manager.notify_connection_status(
                            "Telegram Broadcaster", False, f"Connection lost: {str(e)}"
                        )
                
                # Check stealth marketer
                if stealth_marketer.client:
                    try:
                        if stealth_marketer.client.is_connected():
                            logger.info("Heartbeat: Stealth Marketer connected ✓")
                        else:
                            logger.error("Heartbeat: Stealth Marketer DISCONNECTED!")
                            await notification_manager.notify_connection_status(
                                "Stealth Marketer", False, "Client disconnected. Manual reconnection required."
                            )
                    except Exception as e:
                        logger.error(f"Heartbeat: Stealth Marketer error: {e}")
                        await notification_manager.notify_connection_status(
                            "Stealth Marketer", False, f"Heartbeat check error: {str(e)}"
                        )
            except Exception as e:
                logger.error(f"Heartbeat monitor error: {e}")
    
    asyncio.create_task(heartbeat_monitor())
    logger.info("Heartbeat monitor started (checks every 30 minutes).")

    # 13. Start Scrape & Invite Loop for Stealth Marketer
    async def stealth_scrape_loop():
        """Runs the Scrape and Invite cycle continuously."""
        while True:
            # We wait 1 hour between full cycles
            await asyncio.sleep(3600)
            try:
                if stealth_marketer and stealth_marketer.is_active and not brain.is_sleep_time():
                    await stealth_marketer.scrape_and_invite_cycle()
            except Exception as e:
                logger.error(f"Stealth scrape loop error: {e}")
                
    asyncio.create_task(stealth_scrape_loop())
    logger.info("Stealth Scrape & Invite loop started.")

    # ========================================
    #   MAIN EVENT LOOP
    # ========================================
    logger.info("Entering main event loop...")
    
    last_post_hour = -1  # Track which hour we last posted in
    last_midnight_reset = brain._get_pkt_now().day
    
    while True:
        try:
            pkt_now = brain._get_pkt_now()
            
            # ---- Midnight Reset ----
            if pkt_now.day != last_midnight_reset:
                brain.reset_daily_counters()
                scraper.seen_hashes.clear()  # Allow re-scraping of feeds
                last_midnight_reset = pkt_now.day
                last_post_hour = -1
            
            # ---- Sleep or Pause Cycle Check ----
            if brain.is_paused:
                # System is paused due to a critical error, just idle
                await asyncio.sleep(60)
                continue
                
            if brain.is_sleep_time():
                await asyncio.sleep(300)  # Check again in 5 minutes
                continue
            
            # ---- Check Post Slot ----
            slot = brain.get_next_post_slot()
            
            if slot and slot["hour"] != last_post_hour and brain.can_post():
                last_post_hour = slot["hour"]
                post_type = slot["type"]
                
                logger.info(f"Post slot triggered: {post_type} at {pkt_now.strftime('%I:%M %p PKT')}")
                
                # Add human jitter (wait 0-10 minutes randomly)
                jitter = random.uniform(0, 600)
                logger.info(f"Applying human jitter: waiting {jitter:.0f}s before posting...")
                await asyncio.sleep(jitter)
                
                # Produce content
                try:
                    if post_type == "morning_brief":
                        package = await content_engine.produce_morning_brief()
                    else:
                        package = await content_engine.produce_content_package()
                    
                    if package:
                        # Broadcast to Telegram
                        if telegram_connected:
                            success = await broadcaster.post(package)
                            if success:
                                brain.record_post()
                                logger.info(f"Post #{brain.posts_today} published successfully!")
                                
                                # Try Reddit if within daily limits
                                if brain.posts_today <= brain.max_posts_today // 2: # Keep reddit volume lower
                                    await reddit_broadcaster.post(package)
                                
                                # Broadcast to Twitter
                                await twitter_broadcaster.post(package)
                            else:
                                await brain.handle_error(
                                    "TelegramBroadcaster",
                                    Exception("Post returned False"),
                                    can_auto_fix=True,
                                    fix_action="Will retry next slot"
                                )
                        else:
                            # Dry run mode — just log the content
                            logger.info(f"[DRY RUN] Would post:\n{package['telegram_text'][:200]}...")
                            brain.record_post()
                    else:
                        logger.warning("Content engine returned empty package.")
                        await brain.handle_error(
                            "ContentEngine",
                            Exception("Empty content package"),
                            can_auto_fix=True,
                            fix_action="Will retry next slot"
                        )
                        
                except Exception as e:
                    logger.error(f"Error during content production: {e}")
                    await brain.handle_error("ContentEngine", e)
            
            # ---- Periodic Metric Ingestion (every 3 hours) ----
            if (brain.last_metric_check is None or 
                (pkt_now - brain.last_metric_check).total_seconds() > 10800):
                brain.last_metric_check = pkt_now
                if telegram_connected:
                    sub_count = await broadcaster.get_subscriber_count()
                    await brain.ingest_metrics(sub_count)
                
                # Log status report
                logger.info(brain.get_status_report())
            
            # ---- Heartbeat ----
            await asyncio.sleep(60)  # Check every minute
            
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received. Shutting down...")
            break
        except Exception as e:
            logger.error(f"Critical error in main loop: {e}", exc_info=True)
            await brain.handle_error("MainLoop", e)
            await asyncio.sleep(60)
    
    # Cleanup
    if telegram_connected:
        await broadcaster.disconnect()
    logger.info("Novi News bot shut down gracefully.")


if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
