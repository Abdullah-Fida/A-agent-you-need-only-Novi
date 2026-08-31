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
from modules.signal_copier import SignalCopier
from modules.reddit_broadcaster import RedditBroadcaster
from modules.twitter_broadcaster import TwitterBroadcaster
from modules.notification_manager import NotificationManager
from modules.growth_engine import GrowthEngine
from modules.buffer_broadcaster import BufferBroadcaster
from modules.fanout import Fanout
from modules.social_syndicator import SocialSyndicator
from modules.indexnow import IndexNow
from modules.evergreen import EvergreenDesk
from modules.stock_photos import StockPhotoFinder
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
    
    # 3. Initialize AI Engines
    # News Agent + everything else share one engine/key pool.
    ai_engine = AIEngine(
        api_keys=config.openrouter_api_keys, db=db,
        provider=config.news_api_provider,
        base_url=config.news_api_base,
        default_model=config.news_model,
        label="NewsAI",
        # A second provider, tried only once every model on the first has
        # failed. One provider and one key was a single point of failure for
        # the entire bot.
        backup_keys=config.backup_api_keys,
        backup_provider=config.backup_api_provider,
        backup_model=config.backup_model,
    )

    # The Article Agent can run on its OWN provider, key and model (OpenRouter,
    # Groq, or any OpenAI-compatible endpoint). Falls back to the shared engine
    # when no dedicated key is configured.
    if config.article_api_keys:
        article_ai = AIEngine(
            api_keys=config.article_api_keys,
            db=db,
            provider=config.article_api_provider,
            base_url=config.article_api_base,
            default_model=config.article_model,
            label="ArticleAI",
            backup_keys=config.backup_api_keys,
            backup_provider=config.backup_api_provider,
            backup_model=config.backup_model,
        )
        logger.info(f"Article Agent has a dedicated AI: {config.article_api_provider} "
                    f"/ {config.article_model or 'provider default'}")
    else:
        article_ai = ai_engine
        logger.info("Article Agent shares the News AI (no ARTICLE_API_KEYS configured).")
    
    # 4. Initialize Modules
    scraper = NewsScraper(db=db)
    
    # 5. Notification Manager — built before the image generator, which needs
    # it to email when the Bing cookie expires.
    notification_manager = NotificationManager(
        sender_email=config.email_sender,
        app_password=config.email_app_password,
        receiver_email=config.email_receiver,
        resend_api_key=config.resend_api_key,
        db=db
    )

    # Let the engines report a retired model by email instead of failing quietly.
    ai_engine.notification_manager = notification_manager
    article_ai.notification_manager = notification_manager

    images_dir = os.path.join(os.path.dirname(__file__), "assets", "generated_images")
    channel_name = config.channel_username or "Novi_Network"
    image_gen = ImageGenerator(output_dir=images_dir, channel_name=channel_name,
                               bing_cookie=config.bing_cookie,
                               notification_manager=notification_manager, db=db)

    # Notifies Bing, Yandex and others the moment an article publishes, rather
    # than waiting for a crawler to find it. Google is not a participant and
    # still discovers articles through the sitemap.
    indexnow = IndexNow(key=config.indexnow_key, site_url=config.site_url, db=db)

    # Explainers, on their own bank of long-tail questions. News
    # rewrites cannot outrank the wire that filed them; explainers
    # compete on depth instead, and keep earning for years.
    stock_photos = StockPhotoFinder()

    content_engine = ContentEngine(
        ai_engine=ai_engine,
        scraper=scraper,
        image_gen=image_gen,
        db=db,
        site_name=config.site_name,
        site_url=config.site_url,
        article_ai=article_ai,
        indexnow=indexnow,
        photos=stock_photos
    )

    evergreen = EvergreenDesk(article_agent=content_engine.article_agent,
                              db=db, photos=stock_photos)

    # 6. Initialize The Brain + Growth Engine
    # (created before the broadcasters so they can enforce Brain-owned limits)
    brain = BotBrain(db=db, weekly_goal=config.weekly_subscriber_goal,
                     notification_manager=notification_manager, config=config)

    evergreen.brain = brain

    growth_engine = GrowthEngine(db=db, weekly_goal=config.weekly_subscriber_goal,
                                 notification_manager=notification_manager)
    brain.growth_engine = growth_engine

    # Restore dashboard settings saved before the last restart, so a redeploy
    # no longer silently reverts every toggle and limit to its default.
    await brain.restore_state()

    # 7. Initialize Reddit Broadcaster
    reddit_broadcaster = RedditBroadcaster(
        client_id=config.reddit_client_id,
        client_secret=config.reddit_client_secret,
        username=config.reddit_username,
        password=config.reddit_password,
        user_agent=config.reddit_user_agent,
        db=db
    )

    # 7b. Initialize Buffer (the transport for Facebook and X/Twitter)
    buffer_broadcaster = BufferBroadcaster(
        access_token=config.buffer_access_token,
        organization_id=config.buffer_organization_id,
        enabled_services=config.buffer_services,
        db=db
    )
    if config.buffer_access_token:
        try:
            await buffer_broadcaster.connect()
        except Exception as e:
            logger.error(f"Buffer connect failed: {e}")

    # A second Buffer login. Buffer's free plan caps channels per account,
    # so Bluesky lives on the account the Pinterest agent already uses. The
    # syndicator searches both and posts through whichever holds the channel,
    # so nothing here has to know which login owns what.
    buffer_secondary = None
    if config.buffer_secondary_token and \
            config.buffer_secondary_token != config.buffer_access_token:
        buffer_secondary = BufferBroadcaster(
            access_token=config.buffer_secondary_token,
            enabled_services=config.buffer_services,
            db=db)
        try:
            await buffer_secondary.connect()
        except Exception as e:
            logger.error(f"Second Buffer account connect failed: {e}")

    # 7c. Social syndication — Facebook and X announce every article the
    # moment it publishes, always with a link back to it. Driven by the
    # ARTICLE schedule, never by the Telegram one: the two carry different
    # stories, so a post fired on the Telegram clock would have nothing to
    # link to. Telegram is not involved here at all.
    syndicator = SocialSyndicator(
        buffers=[b for b in (buffer_broadcaster, buffer_secondary) if b],
        db=db,
        growth=growth_engine,
        site_url=config.site_url,
        services=config.buffer_services,
        caps={"facebook": config.social_max_per_day_facebook,
              "twitter": config.social_max_per_day_twitter,
              "threads": config.social_max_per_day_threads,
              "bluesky": config.social_max_per_day_bluesky},
        start_date=config.social_start_date,
        # The brain owns the on/off switches and the date the accounts first
        # posted, so both survive a redeploy.
        brain=brain,
    )

    # 8. Initialize Twitter Broadcaster
    twitter_broadcaster = TwitterBroadcaster(
        username=config.twitter_username,
        password=config.twitter_password,
        email=config.twitter_email,
        telegram_channel=config.channel_username,
        db=db,
        brain=brain
    )

    # 9. Initialize Stealth Marketer (starts INACTIVE — controlled from dashboard)
    stealth_marketer = StealthMarketer(
        api_id=config.telegram_api_id,
        api_hash=config.telegram_api_hash,
        stealth_phone=config.stealth_phone,
        ai_engine=ai_engine,
        brain=brain,
        channel_username=config.channel_username,
        stealth_invite_group=config.stealth_invite_group,
        target_groups=config.target_stealth_groups,
        engagement_rate=0.01,
        notification_manager=notification_manager,
        db=db,
        session_string=config.stealth_session_string or config.telegram_session_string
    )
    # Apply the configured daily invite cap (respects the safety hard cap)
    stealth_marketer.set_daily_invite_limit(config.max_daily_invites)

    # 9b. Cross-platform fan-out (Reddit + X + Facebook + website article).
    # Both the scheduler and NOVI's "post now" go through this, so every
    # publishing route behaves identically.
    fanout = Fanout(
        brain=brain,
        db=db,
        growth_engine=growth_engine,
        reddit=reddit_broadcaster,
        twitter=twitter_broadcaster,
        article_agent=content_engine.article_agent,
        syndicator=syndicator,
        notification_manager=notification_manager,
        # The website publishes on its own schedule, so it needs its own way
        # to find a story and choose a section.
        scraper=scraper,
        pick_category=lambda: content_engine._select_category(
            content_engine._current_hour_pkt()),
    )

    # 10. Connect Telegram Broadcaster, Stealth Marketer & Signal Copier
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
    
    # Signal Copier — posts directly to the Whale Tracker VIP group
    signal_copier = SignalCopier(
        api_id=config.telegram_api_id,
        api_hash=config.telegram_api_hash,
        session_string=config.stealth_session_string or config.telegram_session_string,
        phone=config.stealth_phone,
        source_channels=config.source_signal_channels,
        ai_engine=ai_engine,
        signal_target_group=config.signal_target_group or config.stealth_invite_group,
        notification_manager=notification_manager,
        db=db,
        channel_username=config.channel_username
    )
    signal_copier.set_daily_limit(config.max_daily_signals)

    try:
        # Connect Telegram Broadcaster (for News Agent posting to @Novi_Network)
        await broadcaster.connect()
        if broadcaster.is_ready:
            telegram_connected = True
            sub_count = await broadcaster.get_subscriber_count()
            brain.week_start_subscribers = sub_count
            brain.current_subscribers = sub_count
            logger.info(f"Initial subscriber count: {sub_count}")
        
        # Connect Stealth Marketer (stays INACTIVE until dashboard toggles it)
        try:
            await stealth_marketer.connect()
            # Do NOT activate — dashboard controls activation
            logger.info("Stealth Marketer connected but INACTIVE (waiting for dashboard toggle).")
        except Exception as e:
            logger.error(f"Failed to connect Stealth Marketer: {e}")
        
        # Connect Signal Copier (starts ACTIVE by default)
        await signal_copier.connect()
        
    except Exception as e:
        logger.error(f"Failed to connect Telegram modules: {e}")
        await brain.handle_error("TelegramInit", e)
    
    # 11. Start API Server
    logger.info("Starting Dashboard API server...")
    # 9c. Pinterest agent (AliExpress products -> Pinterest, via its own
    # Buffer account). Its own folder, own AI key, own Buffer token; the only
    # thing it shares with Novi is Supabase. Off unless switched on from the
    # dashboard.
    pin_agent = None
    try:
        from pin_agent.config import load_pin_config
        from pin_agent.pin_bot import PinAgent

        pin_config = load_pin_config()
        if pin_config.ai_api_keys:
            pin_ai = AIEngine(
                api_keys=pin_config.ai_api_keys, db=db,
                provider=pin_config.ai_provider,
                base_url=pin_config.ai_base_url,
                default_model=pin_config.ai_model,
                label="PinAI",
            )
            pin_ai.notification_manager = notification_manager
            logger.info(f"Pinterest agent has a dedicated AI: {pin_config.ai_provider} "
                        f"/ {pin_config.ai_model or 'provider default'}")
        else:
            pin_ai = ai_engine
            logger.info("Pinterest agent shares the News AI (no PIN_AI_KEYS configured).")

        # The pin agent gets its OWN Supabase project when one is configured.
        # The free tier allows 1 GB of file storage per project, and article
        # heroes at eight a day beside pin images at four a day fill a shared
        # bucket inside a year. Two projects give each a full gigabyte, and a
        # problem with one cannot reach the other.
        pin_db = db
        if pin_config.supabase_url and pin_config.supabase_key:
            # Only pin_posts lives there. Checking Novi's eight tables in
            # the pin project would report every one of them missing.
            pin_db = SupabaseDB(pin_config.supabase_url, pin_config.supabase_key,
                                required_tables=["pin_posts"])
            await pin_db.initialize()
            if pin_db._initialized:
                logger.info("Pinterest agent has its own Supabase project.")
            else:
                logger.error("PIN_SUPABASE_URL is set but the project could "
                             "not be reached. Falling back to the shared one.")
                pin_db = db
        else:
            logger.info("Pinterest agent shares Novi's Supabase "
                        "(PIN_SUPABASE_URL not set).")

        async def _upload_pin_image(path: str) -> str:
            # Buffer fetches the image itself, so a pin cannot publish until
            # its picture is hosted somewhere public.
            return await pin_db.upload_image(path, bucket="pin-images")

        pin_agent = PinAgent(
            config=pin_config,
            ai_engine=pin_ai,
            supabase_client=getattr(pin_db, "client", None),
            notification_manager=notification_manager,
            image_dir=os.path.join(os.path.dirname(__file__), "assets", "pins"),
            upload_image=_upload_pin_image,
        )
        if pin_config.buffer_token:
            await pin_agent.connect()
    except Exception as e:
        logger.error(f"Pinterest agent could not be initialised: {type(e).__name__}: {e}")
        pin_agent = None

    api_app.state.pin_agent = pin_agent
    api_app.state.brain = brain
    api_app.state.notification_manager = notification_manager
    api_app.state.stealth_marketer = stealth_marketer
    api_app.state.signal_copier = signal_copier
    api_app.state.content_engine = content_engine
    api_app.state.telegram_broadcaster = broadcaster
    api_app.state.growth_engine = growth_engine
    api_app.state.reddit_broadcaster = reddit_broadcaster
    api_app.state.twitter_broadcaster = twitter_broadcaster
    api_app.state.buffer_broadcaster = buffer_broadcaster
    api_app.state.syndicator = syndicator
    api_app.state.fanout = fanout
    api_app.state.db = db
    api_app.state.config = config
    
    server_port = int(os.environ.get("PORT", 8000))
    config_uv = uvicorn.Config(app=api_app, host="0.0.0.0", port=server_port, loop="asyncio", log_level="warning")
    server = uvicorn.Server(config_uv)
    api_task = asyncio.create_task(server.serve())

    # 12. Start Keep-Alive Self-Ping (Render Free Tier)
    async def self_ping_loop():
        """
        Keeps the Render free-tier instance from idling.

        Render spins a free instance down after ~15 minutes without inbound
        traffic, which is the real cause of "sometimes it just doesn't work".
        We ping every 4 minutes (well inside that window) and, critically,
        ping IMMEDIATELY on startup rather than waiting 10 minutes first.

        A self-ping only helps if it goes through the public URL — pinging
        localhost never generates the inbound request Render measures.
        """
        import aiohttp

        render_url = (os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
        if render_url:
            ping_url = f"{render_url}/ping"
        else:
            ping_url = f"http://localhost:{server_port}/ping"
            logger.warning(
                "RENDER_EXTERNAL_URL is not set — self-ping will hit localhost, which does "
                "NOT prevent Render from idling. Set RENDER_EXTERNAL_URL to your public URL."
            )

        consecutive_failures = 0
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(ping_url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                        if r.status == 200:
                            consecutive_failures = 0
                            logger.debug("Keep-alive self-ping successful.")
                        else:
                            consecutive_failures += 1
                            logger.warning(f"Keep-alive ping returned HTTP {r.status}.")
            except Exception as e:
                consecutive_failures += 1
                logger.warning(f"Keep-alive self-ping failed: {type(e).__name__}")

            # Escalate only once, so we don't spam the inbox while Render is down
            if consecutive_failures == 5:
                await notification_manager.send_notification(
                    subject="Keep-Alive Failing — Bot May Be Idling",
                    message=(
                        f"The self-ping to {ping_url} has failed 5 times in a row.\n\n"
                        f"On Render's free tier this usually means the instance is being "
                        f"spun down, which is why the bot appears to stop working at random."
                    ),
                    is_critical=True
                )

            await asyncio.sleep(240)  # 4 minutes — safely inside Render's ~15 min idle window

    asyncio.create_task(self_ping_loop())
    logger.info("Keep-alive self-ping loop started (every 4 minutes, pings immediately).")
    
    # 13. Heartbeat Monitor
    async def heartbeat_monitor():
        """
        Watches every Telegram client and actively RECONNECTS dead ones.

        Previously this only logged and emailed that a module was down, so a
        dropped connection stayed dropped until someone manually restarted the
        service — a major reason modules "sometimes don't work" in production.
        """
        # Remember what we already alerted on, so a module that stays down
        # doesn't email every 5 minutes.
        alerted = set()

        async def check(name: str, module, reconnect):
            client = getattr(module, "client", None)
            if client is None:
                return
            try:
                if client.is_connected():
                    if name in alerted:
                        alerted.discard(name)
                        await notification_manager.notify_connection_status(
                            name, True, "Connection restored automatically.")
                    logger.info(f"Heartbeat: {name} connected ✓")
                    return

                logger.error(f"Heartbeat: {name} DISCONNECTED — attempting reconnect...")
                try:
                    await reconnect()
                except Exception as e:
                    logger.error(f"Heartbeat: {name} reconnect attempt raised {type(e).__name__}: {e}")

                client = getattr(module, "client", None)
                if client is not None and client.is_connected():
                    logger.info(f"Heartbeat: {name} reconnected successfully ✓")
                    if name in alerted:
                        alerted.discard(name)
                        await notification_manager.notify_connection_status(
                            name, True, "Connection restored automatically.")
                elif name not in alerted:
                    alerted.add(name)
                    await notification_manager.notify_connection_status(
                        name, False, "Connection lost and automatic reconnect failed.")
            except Exception as e:
                logger.error(f"Heartbeat: {name} check error: {type(e).__name__}: {e}")

        while True:
            await asyncio.sleep(300)  # every 5 minutes — catches drops far sooner than 30
            try:
                await check("Telegram Broadcaster", broadcaster, broadcaster.connect)
                await check("Stealth Marketer", stealth_marketer, stealth_marketer.connect)
                await check("Signal Copier", signal_copier, signal_copier.connect)
            except Exception as e:
                logger.error(f"Heartbeat monitor error: {e}")

    asyncio.create_task(heartbeat_monitor())
    logger.info("Heartbeat monitor started (checks + auto-reconnects every 5 minutes).")

    # 14. Stealth Scrape & Invite Loop (respects dashboard toggles)
    async def stealth_scrape_loop():
        while True:
            await asyncio.sleep(3600)  # 1 hour between cycles
            try:
                if (stealth_marketer and stealth_marketer.is_active 
                    and stealth_marketer._scraping_active
                    and not brain.is_sleep_time() 
                    and not brain.master_kill):
                    await stealth_marketer.scrape_and_invite_cycle()
            except Exception as e:
                logger.error(f"Stealth scrape loop error: {e}")
                
    asyncio.create_task(stealth_scrape_loop())
    logger.info("Stealth Scrape & Invite loop started (dashboard-controlled).")

    # ========================================
    #   MAIN EVENT LOOP
    # ========================================
    logger.info("Entering main event loop...")
    
    # Track which slots we already fired TODAY by unique key, so two slots in
    # the same hour both run and a restart doesn't re-fire a done slot.
    fired_slots = set()
    last_pin_at = None
    last_midnight_reset = brain._get_pkt_now().day
    
    while True:
        try:
            pkt_now = brain._get_pkt_now()
            
            # ---- Midnight Reset ----
            if pkt_now.day != last_midnight_reset:
                brain.reset_daily_counters()
                scraper.seen_hashes.clear()
                last_midnight_reset = pkt_now.day
                fired_slots.clear()
                # Reset daily counters on the sub-modules too
                if signal_copier:
                    signal_copier.signals_copied_today = 0
                if stealth_marketer:
                    stealth_marketer.reset_daily_counters()
                if pin_agent:
                    pin_agent.reset_daily()
                    # Re-read what has actually performed, so the next day's
                    # picks are biased by the last 30 days rather than by
                    # whatever was true when the process started.
                    try:
                        pin_agent.selector.performance =                             await pin_agent.store.category_performance()
                    except Exception as e:
                        logger.warning(f"Could not refresh pin performance: "
                                       f"{type(e).__name__}")
            
            # ---- Master Kill Check ----
            if brain.master_kill:
                await asyncio.sleep(60)
                continue
            
            # ---- Sleep or Pause Check ----
            if brain.is_paused:
                await asyncio.sleep(60)
                continue
                
            # ---- The website's own publishing run ----
            # Independent of the Telegram slots and of the sleep window: a web
            # page has no plausible-hours problem, and tying articles to the
            # channel meant the whole US afternoon and evening could never
            # carry one.
            article_slot = brain.get_due_article_slot()
            if article_slot and article_slot["key"] not in fired_slots:
                fired_slots.add(article_slot["key"])
                try:
                    published = await fanout.publish_scheduled_article()
                    if published:
                        logger.info(f"Website article live: /{published.get('slug')}")
                    else:
                        # Nothing published: either deferred, or every
                        # candidate was already written up. Free the slot so
                        # the next pass through this hour can try again.
                        fired_slots.discard(article_slot["key"])
                except Exception as e:
                    logger.error(f"Scheduled article failed: {type(e).__name__}: {e}")
                    fired_slots.discard(article_slot["key"])

            # ---- Evergreen explainer ----
            # Same exemption as the news articles: the website does not sleep.
            ever_slot = brain.get_due_evergreen_slot()
            if ever_slot and ever_slot["key"] not in fired_slots:
                fired_slots.add(ever_slot["key"])
                try:
                    piece = await evergreen.publish_one()
                    if piece:
                        logger.info(f"Evergreen live: /{piece.get('slug')}")
                        # Explainers are announced like any other article.
                        await fanout.syndicate(piece)
                    else:
                        fired_slots.discard(ever_slot["key"])
                except Exception as e:
                    logger.error(f"Evergreen failed: {type(e).__name__}: {e}")
                    fired_slots.discard(ever_slot["key"])

            # ---- Deferred website articles ----
            # A story held back because it had no picture, or because it
            # failed the pre-publish quality check, is rewritten once its
            # delay has elapsed. Independent of the post slots: the Telegram
            # post already went out, only the article is outstanding.
            try:
                await fanout.retry_due_articles()
            except Exception as e:
                logger.error(f"Deferred article pass failed: {type(e).__name__}: {e}")

            # ---- Sleep applies to TELEGRAM ONLY, from here down ----
            # The website runs ABOVE this line deliberately. The sleep window
            # exists so the Telegram account looks like a person who goes to
            # bed; a web page has no such problem, and two of the website's six
            # slots (01:00 and 04:00 PKT) live inside this window. Moving the
            # website block below this gate silently disables them -- which is
            # exactly what happened, and is why there is now a test on the
            # ORDER of this loop rather than only on the brain method.
            if brain.is_sleep_time():
                await asyncio.sleep(300)
                continue
            
            # ---- News Agent Post Slot (only if news_module_active) ----
            if brain.news_module_active:
                slot = brain.get_next_post_slot()

                # A post deferred earlier (usually because no real photograph
                # could be produced) takes priority once its delay elapses.
                # due_retry() pops the entry, so it is only called when the
                # post can actually be attempted — otherwise the deferred post
                # would be dropped by the very check meant to protect it.
                retry = brain.due_retry() if brain.can_post() else None
                if retry:
                    slot = {"key": retry["key"], "type": retry["type"]}
                    attempt_no = retry["attempts"] + 1
                    logger.info(f"Retrying deferred post {retry['key']} "
                                f"(attempt {attempt_no + 1}) — was: {retry['reason']}")
                elif slot and slot["key"] not in fired_slots and brain.can_post():
                    attempt_no = 0
                else:
                    slot = None

                if slot:
                    fired_slots.add(slot["key"])
                    post_type = slot["type"]

                    logger.info(f"News Post slot triggered: {post_type} at {pkt_now.strftime('%I:%M %p PKT')}")

                    # Keep the jitter strictly inside the slot window so a post
                    # can never be jittered past its own deadline. A retry is
                    # already late, so it goes out without further delay.
                    if attempt_no == 0:
                        max_jitter = max(0, (brain.SLOT_WINDOW_MINUTES - 12) * 60)
                        jitter = random.uniform(0, min(600, max_jitter))
                        logger.info(f"Applying human jitter: waiting {jitter:.0f}s before posting...")
                        await asyncio.sleep(jitter)

                    try:
                        if post_type == "morning_brief":
                            package = await content_engine.produce_morning_brief()
                        else:
                            package = await content_engine.produce_content_package()

                        # Every post must carry a real picture. The generator
                        # falls back to a drawn card when Bing fails and the
                        # story has no photograph of its own; rather than
                        # publish that, the post is deferred and tried again
                        # with fresh stories. The card is only accepted once
                        # the attempt budget is spent, so a slot is never lost.
                        deferred = False
                        if package and package.get("image_source") == "card":
                            if brain.queue_retry(slot["key"], post_type, attempt_no,
                                                 "no real image — only a fallback card"):
                                fired_slots.discard(slot["key"])
                                package = None
                                deferred = True
                            else:
                                logger.warning("Publishing with the branded card: "
                                               "retries exhausted and the slot would "
                                               "otherwise be lost.")

                        if package:
                            if telegram_connected:
                                success = await broadcaster.post(package)
                                if success:
                                    brain.record_post(
                                        category=package.get("category", "general"),
                                        topic=package.get("original_title", "")
                                    )
                                    growth_engine.record_action(
                                        growth_engine.ACTION_POST,
                                        {"category": package.get("category")}
                                    )
                                    logger.info(f"News Post #{brain.posts_today} published!")

                                    # Cross-post everywhere else (website, Reddit, X, Facebook)
                                    await fanout.distribute(package, story=package.get("story"))
                                else:
                                    if brain.queue_retry(slot["key"], post_type, attempt_no,
                                                         "Telegram rejected the post"):
                                        fired_slots.discard(slot["key"])
                                    await brain.handle_error(
                                        "TelegramBroadcaster",
                                        Exception("Post returned False"),
                                        can_auto_fix=True,
                                        fix_action=f"Retrying in {brain.RETRY_DELAY_MINUTES} min"
                                    )
                            else:
                                logger.info(f"[DRY RUN] Would post:\n{package['telegram_text'][:200]}...")
                                brain.record_post()
                        elif not deferred:
                            logger.warning("Content engine returned empty package.")
                            if brain.queue_retry(slot["key"], post_type, attempt_no,
                                                 "content engine produced nothing"):
                                fired_slots.discard(slot["key"])

                    except Exception as e:
                        logger.error(f"Error during news production: {e}")
                        if brain.queue_retry(slot["key"], post_type, attempt_no, str(e)[:80]):
                            fired_slots.discard(slot["key"])
                        await brain.handle_error("ContentEngine", e)

            
            # ---- Pinterest agent ----
            # Spaced deliberately: Pinterest reads a burst of pins as
            # automation, and Buffer's free queue holds ten per channel.
            if (brain.pin_module_active and pin_agent
                    and (last_pin_at is None
                         or (pkt_now - last_pin_at).total_seconds()
                         >= pin_agent.config.min_minutes_between_pins * 60)):
                last_pin_at = pkt_now
                try:
                    pin = await pin_agent.run_once()
                    if pin:
                        logger.info(f"Pin {pin.get('status')}: {pin['title'][:50]}")
                except Exception as e:
                    logger.error(f"Pin agent cycle failed: {type(e).__name__}: {e}")
                    await brain.handle_error("PinAgent", e)

            # ---- Periodic Metric Ingestion (every 3 hours) ----
            if (brain.last_metric_check is None or 
                (pkt_now - brain.last_metric_check).total_seconds() > 10800):
                brain.last_metric_check = pkt_now
                if telegram_connected:
                    sub_count = await broadcaster.get_subscriber_count()
                    await brain.ingest_metrics(sub_count)
                    await growth_engine.record_subscribers(sub_count)

                logger.info(brain.get_status_report())
            
            # ---- Heartbeat ----
            await asyncio.sleep(60)
            
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
    if signal_copier:
        await signal_copier.disconnect()
    logger.info("Novi News bot shut down gracefully.")


if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
