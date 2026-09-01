"""
The Binance Square agent.

    market data  ->  score  ->  write  ->  gate  ->  Telegram draft  ->  you paste

Everything up to the paste is automatic. The paste is not automatable:
Binance Square has no posting API, and driving their web UI with a browser
would breach their terms on an account that holds money. Ninety seconds a
day is the honest price of that, and it is worth paying.

OFF by default, like every other agent here.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from binance_agent.delivery import DraftDelivery
from binance_agent.market import BinanceMarket
from binance_agent.writer import PostWriter

logger = logging.getLogger("BinanceAgent")

PKT = timedelta(hours=5)

# Drafts land when a person is awake to paste them, not when a scheduler
# feels like it. Crypto never closes, so the constraint is the human.
#     PKT     UTC     what
DRAFT_SLOTS = [
    (10, 0),   # 05:00   morning, before the day starts
    (16, 0),   # 11:00   afternoon
    (22, 0),   # 17:00   evening
]
SLOT_WINDOW_MINUTES = 25


class BinanceAgent:
    """Finds a story in the market, writes it, and hands it to you."""

    def __init__(self, config, ai_engine=None, client_owner=None, db=None):
        self.config = config
        self.db = db
        self.market = BinanceMarket(min_quote_volume=config.min_volume_usd)
        self.writer = PostWriter(ai_engine=ai_engine, brand=config.brand)
        self.delivery = DraftDelivery(client_owner=client_owner,
                                      group=config.draft_group)

        self.active = False               # switched on from the dashboard
        self.drafted_today = 0
        self._counter_day = None
        self._recent: List[Dict] = []     # [{base, at}] for the repeat guard
        self.last_run: Optional[datetime] = None
        self.last_error = ""

    # ── time ─────────────────────────────────────────────────────

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc) + PKT

    def _roll_day(self) -> None:
        today = self._now().date()
        if self._counter_day != today:
            self._counter_day = today
            self.drafted_today = 0

    def due_slot(self) -> Optional[Dict]:
        now = self._now()
        for hour, minute in DRAFT_SLOTS:
            if (now.hour == hour and
                    minute <= now.minute < minute + SLOT_WINDOW_MINUTES):
                return {"hour": hour, "minute": minute,
                        "key": f"binance_{hour}_{minute}"}
        return None

    # ── the repeat guard ─────────────────────────────────────────

    def _too_soon(self, base: str) -> bool:
        """
        Whether this coin was written about recently.

        Without it BTC and ETH win the ranking most days -- they carry the
        volume -- and the feed becomes the same two posts forever.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=self.config.repeat_after_days)
        self._recent = [r for r in self._recent if r["at"] > cutoff]
        return any(r["base"] == base for r in self._recent)

    # ── the run ──────────────────────────────────────────────────

    async def build_one(self) -> Optional[Dict]:
        """Finds the best story not covered recently, and writes it."""
        movers = await self.market.movers(limit=10)
        if not movers:
            self.last_error = self.market.last_error or "no market data"
            logger.warning(f"Nothing to write about: {self.last_error}")
            return None

        for coin in movers:
            if self._too_soon(coin["base"]):
                continue
            # A move under 3% is not a story, however big the volume.
            if abs(coin["change"]) < 3.0:
                continue

            ctx = await self.market.context(coin["symbol"])
            draft = await self.writer.write(coin, ctx)
            if not draft:
                self.last_error = self.writer.last_error or "the gate refused it"
                logger.warning(f"${coin['base']}: {self.last_error}")
                continue

            self._recent.append({"base": coin["base"],
                                 "at": datetime.now(timezone.utc)})
            return draft

        self.last_error = ("nothing moved more than 3% on real volume that "
                           "has not been covered in the last few days")
        logger.info(self.last_error)
        return None

    async def run_slot(self) -> Optional[Dict]:
        """One scheduled run: build a draft and deliver it."""
        if not self.active:
            logger.info("Binance agent is OFF — skipping.")
            return None

        self._roll_day()
        if self.drafted_today >= self.config.drafts_per_day:
            logger.info(f"Binance: {self.drafted_today} drafts already today.")
            return None

        self.last_run = datetime.now(timezone.utc)
        draft = await self.build_one()
        if not draft:
            # Said out loud rather than swallowed: a quiet market is a fact
            # worth knowing, and silence is indistinguishable from a crash.
            await self.delivery.announce(
                f"No Binance draft this slot — {self.last_error}.")
            return None

        ok = await self.delivery.send(draft, index=self.drafted_today + 1,
                                      total=self.config.drafts_per_day)
        if ok:
            self.drafted_today += 1
            if self.db:
                try:
                    await self.db.log_post(
                        platform="binance_square", content=draft["text"],
                        status="drafted",
                        metadata={"symbol": draft["symbol"],
                                  "score": draft["score"]})
                except Exception:
                    pass
        return draft if ok else None

    @property
    def status(self) -> Dict:
        self._roll_day()
        return {
            "active": self.active,
            "drafted_today": self.drafted_today,
            "per_day": self.config.drafts_per_day,
            "slots_pkt": [f"{h:02d}:{m:02d}" for h, m in DRAFT_SLOTS],
            "market": self.market.status,
            "delivery": self.delivery.status,
            "recently_covered": [r["base"] for r in self._recent],
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "last_error": self.last_error,
        }
