"""Configuration for the Binance Square agent."""
import logging
import os
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger("BinanceAgent.Config")


def _clean(v: str) -> str:
    v = (v or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1].strip()
    return v


def _csv(name: str) -> List[str]:
    return [p for p in (_clean(x) for x in os.getenv(name, "").split(",")) if p]


def _int(name: str, default: int, lo: int = None, hi: int = None) -> int:
    raw = _clean(os.getenv(name, ""))
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        logger.warning(f"{name}='{raw}' is not a number. Using {default}.")
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


@dataclass
class BinanceConfig:
    # Where the drafts land. A Telegram group the account is a MEMBER of;
    # supergroup ids look like -100XXXXXXXXXX. Falls back to the signal
    # group so this works with nothing new configured.
    draft_group: str = ""

    # Binance Square has no posting API -- publishing is a manual paste, so
    # three a day is about ninety seconds of work. More than that is not
    # rewarded: Square ranks on engagement, and ten thin posts earn less
    # than two good ones.
    drafts_per_day: int = 3

    # Below this a percentage move is one buyer in a thin book, not a story.
    min_volume_usd: float = 20_000_000.0

    # A coin is not written about twice inside this many days, however hard
    # it moves. Otherwise BTC would be every post.
    repeat_after_days: int = 3

    ai_api_keys: List[str] = field(default_factory=list)
    ai_provider: str = "groq"
    ai_base_url: str = ""
    ai_model: str = ""

    brand: str = "PressVane"
    referral_note: str = ""


def load_binance_config() -> BinanceConfig:
    return BinanceConfig(
        draft_group=(_clean(os.getenv("BINANCE_DRAFT_GROUP", ""))
                     or _clean(os.getenv("SIGNAL_TARGET_GROUP", ""))),
        drafts_per_day=_int("BINANCE_DRAFTS_PER_DAY", 3, lo=0, hi=8),
        min_volume_usd=float(_int("BINANCE_MIN_VOLUME_M", 20, lo=1)) * 1_000_000,
        repeat_after_days=_int("BINANCE_REPEAT_AFTER_DAYS", 3, lo=1),
        ai_api_keys=_csv("BINANCE_AI_KEYS") or _csv("ARTICLE_API_KEYS"),
        ai_provider=_clean(os.getenv("BINANCE_AI_PROVIDER", "")) or "groq",
        ai_base_url=_clean(os.getenv("BINANCE_AI_BASE", "")),
        ai_model=_clean(os.getenv("BINANCE_AI_MODEL", "")) or "openai/gpt-oss-20b",
        brand=_clean(os.getenv("SITE_NAME", "")) or "PressVane",
        referral_note=_clean(os.getenv("BINANCE_REFERRAL_NOTE", "")),
    )
