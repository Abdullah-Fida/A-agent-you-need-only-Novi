"""
Configuration Manager for the Omni-Channel Bot.
Loads environment variables and provides validated access to all settings.
"""
import os
import logging
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv

logger = logging.getLogger("OmniBot.Config")

@dataclass
class BotConfig:
    """Immutable configuration object loaded from .env"""
    
    # Supabase
    supabase_url: str = ""
    supabase_key: str = ""
    
    # AI — News Agent / everything else (Multi-Key)
    openrouter_api_keys: List[str] = field(default_factory=list)
    news_api_provider: str = "openrouter"   # openrouter | groq | openai
    news_api_base: str = ""
    news_model: str = ""
    bing_cookie: str = ""

    # AI — Article Agent (its own provider/key/model, kept separate so the
    # blog writer can run on a stronger paid model without touching the rest)
    article_api_keys: List[str] = field(default_factory=list)
    article_api_provider: str = "openrouter"   # openrouter | groq | openai
    article_api_base: str = ""                 # explicit override
    article_model: str = ""

    # Telegram
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_phone: str = ""
    telegram_session_string: str = ""
    stealth_phone: str = ""  # Burner phone for stealth marketer
    stealth_session_string: str = ""
    channel_username: str = ""
    stealth_invite_group: str = ""
    target_stealth_groups: List[str] = field(default_factory=list)
    source_signal_channels: List[str] = field(default_factory=list)
    signal_target_group: str = ""
    
    # Twitter (X) Browser Automation
    twitter_username: str = ""
    twitter_password: str = ""
    twitter_email: str = ""
    
    # Buffer (Facebook and other social channels)
    buffer_access_token: str = ""
    buffer_organization_id: str = ""
    buffer_services: List[str] = field(default_factory=lambda: ["facebook"])

    # Website
    site_url: str = ""
    site_name: str = "Novi News"

    # Reddit
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_username: str = ""
    reddit_password: str = ""
    reddit_user_agent: str = ""
    
    # Brain Goals & daily caps (all of these are enforced at runtime)
    weekly_subscriber_goal: int = 100
    max_daily_posts: int = 6
    max_daily_reddit_posts: int = 2
    max_daily_x_posts: int = 5
    max_daily_telegram_replies: int = 8
    max_daily_invites: int = 2
    max_daily_signals: int = 0  # 0 = unlimited

    # Sleep window (PKT, 24h). Set both equal for 24/7 operation.
    sleep_start_hour: int = 23
    sleep_end_hour: int = 7

    # Notifications
    email_sender: str = ""
    email_app_password: str = ""
    email_receiver: str = ""
    resend_api_key: str = ""

def _news_keys(openrouter_keys):
    """
    The keys that match the provider the News AI is pointed at.

    NEWS_API_PROVIDER and the key list were independent, so switching the
    provider to groq kept sending an OpenRouter key and every call failed
    authentication. The provider now decides which key is used.
    """
    provider = (_clean(os.getenv("NEWS_API_PROVIDER", "")) or "openrouter").lower()
    if provider == "groq":
        groq = _csv("NEWS_API_KEYS") or _csv("GROQ_API_KEY")
        if groq:
            logger.info(f"News AI uses {len(groq)} Groq key(s).")
            return groq
        logger.error("NEWS_API_PROVIDER=groq but no GROQ_API_KEY is set; "
                     "falling back to the OpenRouter keys, which will not "
                     "authenticate against Groq.")
    elif provider == "openai":
        openai_keys = _csv("NEWS_API_KEYS") or _csv("OPENAI_API_KEY")
        if openai_keys:
            return openai_keys
    return _csv("NEWS_API_KEYS") or openrouter_keys


def load_config() -> BotConfig:
    """Loads configuration from .env file and returns a BotConfig object."""
    load_dotenv()
    
    # Parse comma-separated API keys.
    # NEWS_API_KEYS is the provider-neutral name (the news agent can run on
    # Groq or OpenAI now); OPENROUTER_API_KEYS is kept for compatibility.
    api_keys = _csv("NEWS_API_KEYS") or _csv("OPENROUTER_API_KEYS")
    
    if not api_keys:
        logger.warning("No OpenRouter API keys found in .env! AI features will not work.")
    else:
        logger.info(f"Loaded {len(api_keys)} OpenRouter API key(s).")
    
    config = BotConfig(
        supabase_url=os.getenv("SUPABASE_URL", ""),
        supabase_key=os.getenv("SUPABASE_KEY", ""),
        openrouter_api_keys=_news_keys(api_keys),
        news_api_provider=_clean(os.getenv("NEWS_API_PROVIDER", "")) or "openrouter",
        news_api_base=_clean(os.getenv("NEWS_API_BASE", "")),
        news_model=_clean(os.getenv("NEWS_MODEL", "")),
        article_api_keys=_csv("ARTICLE_API_KEYS") or _csv("ARTICLE_API_KEY"),
        article_api_provider=_clean(os.getenv("ARTICLE_API_PROVIDER", "")) or "openrouter",
        article_api_base=_clean(os.getenv("ARTICLE_API_BASE", "")),
        article_model=_clean(os.getenv("ARTICLE_MODEL", "")),
        bing_cookie=_clean(os.getenv("BING_COOKIE", "")),
        telegram_api_id=_int_env("TELEGRAM_API_ID", 0),
        telegram_api_hash=os.getenv("TELEGRAM_API_HASH", ""),
        telegram_phone=os.getenv("TELEGRAM_PHONE", ""),
        telegram_session_string=os.getenv("TELEGRAM_SESSION_STRING", ""),
        stealth_phone=os.getenv("STEALTH_PHONE", ""),
        stealth_session_string=os.getenv("STEALTH_SESSION_STRING", ""),
        channel_username=_clean(os.getenv("CHANNEL_USERNAME", "")),
        stealth_invite_group=_clean(os.getenv("STEALTH_INVITE_GROUP", "")),
        target_stealth_groups=_csv("TARGET_STEALTH_GROUPS"),
        source_signal_channels=_csv("SOURCE_SIGNAL_CHANNELS"),
        signal_target_group=_clean(os.getenv("SIGNAL_TARGET_GROUP", "")),
        twitter_username=os.getenv("TWITTER_USERNAME", ""),
        twitter_password=os.getenv("TWITTER_PASSWORD", ""),
        twitter_email=os.getenv("TWITTER_EMAIL", ""),
        buffer_access_token=_clean(os.getenv("BUFFER_ACCESS_TOKEN", "")),
        buffer_organization_id=_clean(os.getenv("BUFFER_ORGANIZATION_ID", "")),
        buffer_services=_csv("BUFFER_SERVICES") or ["facebook"],
        site_url=_clean(os.getenv("SITE_URL", "")),
        site_name=_clean(os.getenv("SITE_NAME", "")) or "Novi News",
        reddit_client_id=os.getenv("REDDIT_CLIENT_ID", ""),
        reddit_client_secret=os.getenv("REDDIT_CLIENT_SECRET", ""),
        reddit_username=os.getenv("REDDIT_USERNAME", ""),
        reddit_password=os.getenv("REDDIT_PASSWORD", ""),
        reddit_user_agent=os.getenv("REDDIT_USER_AGENT", ""),
        weekly_subscriber_goal=_int_env("WEEKLY_SUBSCRIBER_GOAL", 100),
        max_daily_posts=_int_env("MAX_DAILY_POSTS", 6),
        max_daily_reddit_posts=_int_env("MAX_DAILY_REDDIT_POSTS", 2),
        max_daily_x_posts=_int_env("MAX_DAILY_X_POSTS", 5),
        max_daily_telegram_replies=_int_env("MAX_DAILY_TELEGRAM_REPLIES", 8),
        max_daily_invites=_int_env("MAX_DAILY_INVITES", 2),
        max_daily_signals=_int_env("MAX_DAILY_SIGNALS", 0),
        sleep_start_hour=_int_env("SLEEP_START_HOUR", 23, lo=0, hi=23),
        sleep_end_hour=_int_env("SLEEP_END_HOUR", 7, lo=0, hi=23),
        email_sender=_clean(os.getenv("EMAIL_SENDER", "")),
        email_app_password=os.getenv("EMAIL_APP_PASSWORD", "").strip(),
        email_receiver=_clean(os.getenv("EMAIL_RECEIVER", "")),
        resend_api_key=_clean(os.getenv("RESEND_API_KEY", "")),
    )

    _warn_on_problems(config)
    return config


def _clean(value: str) -> str:
    """Strips whitespace and stray surrounding quotes from an env value."""
    v = (value or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1].strip()
    return v


def _csv(name: str) -> List[str]:
    """Parses a comma-separated env var into a clean list."""
    return [p for p in (_clean(x) for x in os.getenv(name, "").split(",")) if p]


def _int_env(name: str, default: int, lo: int = None, hi: int = None) -> int:
    """
    Reads an int env var without crashing the whole bot on a typo.
    A bad value falls back to the default and logs a clear warning.
    """
    raw = _clean(os.getenv(name, ""))
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        logger.warning(f"{name}='{raw}' is not a valid integer. Using default {default}.")
        return default
    if lo is not None and val < lo:
        logger.warning(f"{name}={val} below minimum {lo}. Clamping.")
        val = lo
    if hi is not None and val > hi:
        logger.warning(f"{name}={val} above maximum {hi}. Clamping.")
        val = hi
    return val


def _warn_on_problems(cfg: "BotConfig"):
    """
    Surfaces misconfiguration at startup instead of letting a module fail
    silently hours later. These are warnings, not crashes — the bot still
    runs with whatever is correctly configured.
    """
    if not cfg.supabase_url or not cfg.supabase_key:
        logger.warning("Supabase not configured — nothing will be persisted to the database.")

    if not cfg.resend_api_key and not (cfg.email_sender and cfg.email_app_password):
        logger.warning("No email method configured — you will NOT receive notifications.")
    elif not cfg.resend_api_key:
        logger.warning("Only Gmail SMTP configured. Render's free tier blocks SMTP ports — "
                       "set RESEND_API_KEY for reliable email in production.")

    if not cfg.telegram_session_string:
        logger.warning("TELEGRAM_SESSION_STRING is empty — the news channel broadcaster "
                       "cannot post in production (file sessions do not survive deploys).")

    # Chat IDs: catch the missing -100 supergroup prefix, the single most common
    # cause of 'the bot silently posts nothing'.
    for label, value in (("STEALTH_INVITE_GROUP", cfg.stealth_invite_group),
                         ("SIGNAL_TARGET_GROUP", cfg.signal_target_group)):
        if value and value.lstrip("-").isdigit():
            digits = value.lstrip("-")
            if not digits.startswith("100"):
                logger.warning(
                    f"{label}={value} looks like a supergroup ID missing the '-100' prefix. "
                    f"It will be auto-corrected to -100{digits} at runtime, but you should "
                    f"fix it in .env to remove any ambiguity."
                )

    if cfg.source_signal_channels and not cfg.signal_target_group:
        logger.warning("Signal source channels are set but SIGNAL_TARGET_GROUP is empty — "
                       "signals would have nowhere to go.")

    if cfg.sleep_start_hour == cfg.sleep_end_hour:
        logger.info("Sleep window disabled — the bot will run 24/7.")

    # Easy trap: setting the provider/model but forgetting the key means the
    # Article Agent quietly keeps using the shared news AI instead.
    if not cfg.article_api_keys and (cfg.article_model or
                                     cfg.article_api_provider not in ("", "openrouter")):
        logger.warning(
            f"ARTICLE_API_PROVIDER='{cfg.article_api_provider}' / "
            f"ARTICLE_MODEL='{cfg.article_model}' are set, but ARTICLE_API_KEYS is EMPTY. "
            f"The Article Agent will fall back to the shared news AI (OpenRouter) and "
            f"will NOT use {cfg.article_api_provider}. Add ARTICLE_API_KEYS to activate it."
        )

    if not os.environ.get("GROQ_API_KEY", "").strip():
        logger.warning("GROQ_API_KEY is not set — the NOVI dashboard's voice assistant "
                       "will not be able to answer.")
