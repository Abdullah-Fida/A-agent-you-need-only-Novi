"""
Live market data from Binance. Read-only, no key, no account.

WHY data-api.binance.vision AND NOT api.binance.com
---------------------------------------------------
Binance geo-blocks US IP addresses on api.binance.com with HTTP 451. The bot
runs on Render in San Francisco, so the obvious host works perfectly on a
laptop in Lahore and fails in production -- the worst kind of bug, because
it only appears where you cannot watch it.

data-api.binance.vision is Binance's public market-data host. Same endpoints,
same data, no key, and no geo-restriction. CoinGecko is the fallback if it
ever goes down.

Nothing here can place an order. There is no API key in this module and no
endpoint that could move money even if there were.
"""
import asyncio
import logging
import math
from typing import Dict, List, Optional

logger = logging.getLogger("BinanceAgent.Market")

PRIMARY = "https://data-api.binance.vision"
FALLBACK = "https://api1.binance.com"
TIMEOUT = 40

# Leveraged tokens move for reasons that have nothing to do with the asset,
# so a post about one would be explaining an artefact.
#
# Matched as a SUFFIX, not a substring. Binance names them BTCUP, ETHDOWN,
# BTC3L, ETHBULL -- the marker is always on the end. A substring test threw
# away SUPER, which contains "UP", and would have quietly excluded any real
# coin whose name happened to hold two of these letters.
EXCLUDE_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S")

# Stablecoins do not "move". A 0.02% wobble on USDC is not a story, and it
# would otherwise dominate any ranking that looks at volume.
STABLECOINS = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USD1", "RLUSD",
    "EUR", "AEUR", "USDE", "PYUSD", "XUSD",
}


class BinanceMarket:
    """Reads public market data. Never raises; returns [] on failure."""

    def __init__(self, min_quote_volume: float = 20_000_000.0):
        # Below this, a percentage move is noise: a thin book can print +40%
        # on a few thousand dollars and mean nothing.
        self.min_quote_volume = min_quote_volume
        self.host = PRIMARY
        self.last_error = ""

    async def _get(self, path: str, params: Dict = None) -> Optional[object]:
        import httpx
        for host in (self.host, FALLBACK):
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                    r = await client.get(f"{host}{path}", params=params or {})
                if r.status_code == 451:
                    # The geo-block. Says nothing about our request being wrong.
                    self.last_error = (f"{host} refused this region (451). "
                                       f"Use data-api.binance.vision.")
                    logger.warning(self.last_error)
                    continue
                if r.status_code != 200:
                    self.last_error = f"{host} returned HTTP {r.status_code}"
                    logger.warning(self.last_error)
                    continue
                self.host = host
                return r.json()
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                logger.warning(f"{host} unreachable: {self.last_error}")
        return None

    @staticmethod
    def _base(symbol: str) -> str:
        return symbol[:-4] if symbol.endswith("USDT") else symbol

    def _tradeable(self, row: Dict) -> bool:
        # The endpoint has returned nulls and bare strings in a list of
        # objects before. One bad entry must not lose the whole run.
        if not isinstance(row, dict):
            return False
        sym = row.get("symbol") or ""
        if not sym.endswith("USDT"):
            return False
        base = self._base(sym)
        if base in STABLECOINS:
            return False
        if any(base.endswith(m) for m in EXCLUDE_SUFFIXES):
            return False
        try:
            return float(row.get("quoteVolume", 0)) >= self.min_quote_volume
        except (TypeError, ValueError):
            return False

    async def movers(self, limit: int = 8) -> List[Dict]:
        """
        The coins worth writing about, best-scored first.

        Ranked on more than the percentage. A 30% move on $2m of volume is
        one buyer; the same move on $29m across 480,000 trades is a market
        doing something. Volume and trade count are log-compressed so a
        single enormous pair cannot own the ranking forever.
        """
        data = await self._get("/api/v3/ticker/24hr")
        if not isinstance(data, list):
            logger.error(f"No market data: {self.last_error}")
            return []

        rows = []
        for row in data:
            if not self._tradeable(row):
                continue
            try:
                change = float(row["priceChangePercent"])
                volume = float(row["quoteVolume"])
                trades = int(row["count"])
                last = float(row["lastPrice"])
                high = float(row["highPrice"])
                low = float(row["lowPrice"])
            except (KeyError, TypeError, ValueError):
                continue

            # abs(): a 12% fall is exactly as interesting as a 12% rise, and
            # ranking only on gainers produces a permanently bullish feed
            # that stops being credible the first week the market drops.
            score = (abs(change)
                     * math.log10(max(volume, 10) / 1e6 + 1)
                     * math.log10(max(trades, 10) / 1e3 + 1))

            rows.append({
                "symbol": row["symbol"],
                "base": self._base(row["symbol"]),
                "change": change,
                "price": last,
                "high": high,
                "low": low,
                "volume": volume,
                "trades": trades,
                "score": round(score, 2),
                # Where in the day's range it closed: 1.0 at the high, 0.0 at
                # the low. The single most useful number for "is this move
                # holding or fading".
                "range_position": ((last - low) / (high - low)) if high > low else 0.5,
            })

        rows.sort(key=lambda r: r["score"], reverse=True)
        logger.info(f"{len(data)} symbols -> {len(rows)} tradeable "
                    f"above ${self.min_quote_volume/1e6:.0f}m volume")
        return rows[:limit]

    async def context(self, symbol: str) -> Dict:
        """
        Where today's move sits against the last month.

        Without this a post can only say "it went up", which the reader can
        see for themselves. With it the post can say whether this is a new
        high or a bounce off the floor, which is the part worth reading.
        """
        candles = await self._get("/api/v3/klines",
                                  {"symbol": symbol, "interval": "1d", "limit": 30})
        if not isinstance(candles, list) or len(candles) < 7:
            return {}
        try:
            highs = [float(c[2]) for c in candles]
            lows = [float(c[3]) for c in candles]
            closes = [float(c[4]) for c in candles]
        except (IndexError, TypeError, ValueError):
            return {}

        month_high, month_low = max(highs), min(lows)
        last = closes[-1]
        week_ago = closes[-8] if len(closes) >= 8 else closes[0]
        month_ago = closes[0]
        return {
            "month_high": month_high,
            "month_low": month_low,
            "week_change": ((last - week_ago) / week_ago * 100) if week_ago else 0.0,
            "month_change": ((last - month_ago) / month_ago * 100) if month_ago else 0.0,
            "at_month_high": last >= month_high * 0.98,
            "at_month_low": last <= month_low * 1.02,
            "days": len(closes),
        }

    @property
    def status(self) -> Dict:
        return {"host": self.host, "last_error": self.last_error,
                "min_quote_volume": self.min_quote_volume}
