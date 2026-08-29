"""
Configuration for the Pinterest agent.

Deliberately separate from Novi's config: this agent has its own AI key, its
own credentials and its own limits, so a change here cannot affect the news
bot. The one thing it shares is the Supabase database.
"""
import logging
import os
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger("PinAgent.Config")


def _clean(value: str) -> str:
    """Strips quotes and any trailing inline comment from an env value."""
    value = (value or "").strip().strip('"').strip("'")
    if "  #" in value:
        value = value.split("  #")[0].strip()
    return value


def _csv(name: str) -> List[str]:
    return [p.strip() for p in _clean(os.getenv(name, "")).split(",") if p.strip()]


def _int(name: str, default: int) -> int:
    try:
        return int(_clean(os.getenv(name, "")) or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_clean(os.getenv(name, "")) or default)
    except ValueError:
        return default


@dataclass
class PinConfig:
    # ── AliExpress ────────────────────────────────────────────────
    ali_app_key: str = ""
    ali_app_secret: str = ""
    ali_tracking_id: str = ""

    # ── Pinterest ─────────────────────────────────────────────────
    pinterest_app_id: str = ""
    pinterest_app_secret: str = ""
    pinterest_access_token: str = ""
    pinterest_refresh_token: str = ""
    pinterest_redirect_uri: str = ""
    # Trial access publishes sandbox pins only its creator can see. Left False
    # until Pinterest grants Standard access, so nothing is posted publicly by
    # accident during testing.
    pinterest_standard_access: bool = False

    # ── AI (its own key, separate from the news bot) ──────────────
    ai_api_keys: List[str] = field(default_factory=list)
    ai_provider: str = "groq"
    ai_base_url: str = ""
    ai_model: str = ""

    # ── Behaviour ─────────────────────────────────────────────────
    niche: str = "home_kitchen"
    pins_per_day: int = 8
    min_minutes_between_pins: int = 45
    # Every pin is reviewed in Telegram before publishing until this is off.
    require_review: bool = True

    # ── Product filters ───────────────────────────────────────────
    min_rating: float = 4.3
    min_orders: int = 100
    min_price: float = 3.0
    max_price: float = 80.0

    site_name: str = "Novi"
    brand_handle: str = "@Novi_Network"


def load_pin_config() -> PinConfig:
    cfg = PinConfig(
        ali_app_key=_clean(os.getenv("ALI_APP_KEY", "")),
        ali_app_secret=_clean(os.getenv("ALI_APP_SECRET", "")),
        ali_tracking_id=_clean(os.getenv("ALI_TRACKING_ID", "")),
        pinterest_app_id=_clean(os.getenv("PINTEREST_APP_ID", "")),
        pinterest_app_secret=_clean(os.getenv("PINTEREST_APP_SECRET", "")),
        pinterest_access_token=_clean(os.getenv("PINTEREST_ACCESS_TOKEN", "")),
        pinterest_refresh_token=_clean(os.getenv("PINTEREST_REFRESH_TOKEN", "")),
        pinterest_redirect_uri=_clean(os.getenv("PINTEREST_REDIRECT_URI", "")),
        pinterest_standard_access=_clean(
            os.getenv("PINTEREST_STANDARD_ACCESS", "")).lower() in ("1", "true", "yes"),
        ai_api_keys=_csv("PIN_AI_KEYS"),
        ai_provider=_clean(os.getenv("PIN_AI_PROVIDER", "")) or "groq",
        ai_base_url=_clean(os.getenv("PIN_AI_BASE", "")),
        ai_model=_clean(os.getenv("PIN_AI_MODEL", "")),
        niche=_clean(os.getenv("PIN_NICHE", "")) or "home_kitchen",
        pins_per_day=_int("PIN_MAX_PER_DAY", 8),
        min_minutes_between_pins=_int("PIN_MIN_GAP_MINUTES", 45),
        require_review=_clean(
            os.getenv("PIN_REQUIRE_REVIEW", "true")).lower() not in ("0", "false", "no"),
        min_rating=_float("PIN_MIN_RATING", 4.3),
        min_orders=_int("PIN_MIN_ORDERS", 100),
        min_price=_float("PIN_MIN_PRICE", 3.0),
        max_price=_float("PIN_MAX_PRICE", 80.0),
        site_name=_clean(os.getenv("SITE_NAME", "")) or "Novi",
        brand_handle=_clean(os.getenv("CHANNEL_USERNAME", "")) or "@Novi_Network",
    )

    # Pinterest recommends 5-15 pins a day; more reads as automation.
    if cfg.pins_per_day > 15:
        logger.warning(f"PIN_MAX_PER_DAY={cfg.pins_per_day} exceeds Pinterest's "
                       f"recommended ceiling of 15; clamping.")
        cfg.pins_per_day = 15

    if not cfg.ai_api_keys:
        logger.warning("PIN_AI_KEYS is not set — the agent will fall back to "
                       "Novi's AI engine if one is supplied.")
    return cfg
