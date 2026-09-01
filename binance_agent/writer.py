"""
Writes the Binance Square post.

WHAT EARNS ON SQUARE, and why the post is shaped this way:

  * $TICKER cashtags. Square makes them clickable and files the post on that
    coin's page, where people already are. It is the single biggest reach
    lever on the platform and it costs one character.
  * A question at the end. Square's ranking rewards comments far more than
    it rewards views, and a post that ends in a full stop gets none.
  * Real numbers. "Bitcoin is pumping" is worthless; the price, the range and
    the trade count are what makes a post worth reading and what stops it
    reading as generated filler.
  * A disclaimer, always. Never advice, never a target, never "buy".

The numbers are computed here and handed to the model as facts. The model is
never asked what the price is -- it is asked to write around numbers it has
been given, because a model asked for a figure will invent one.
"""
import logging
import re
from datetime import datetime, timezone
from typing import Dict, Optional

logger = logging.getLogger("BinanceAgent.Writer")

DISCLAIMER = "Not financial advice. Do your own research."

# Anything that turns an observation into a recommendation. The whole
# difference between a post that is fine and a post that is a liability.
_ADVICE = re.compile(
    r"\b(buy now|you should buy|sell now|you should sell|guaranteed|"
    r"will definitely|sure thing|can't lose|cannot lose|to the moon|"
    r"financial advice|i recommend|my advice|risk[- ]free|easy money|"
    r"100x|1000x|get rich)\b", re.I)

# A price target is a prediction, and a prediction ages into a false claim.
_TARGET = re.compile(r"\b(target|tp|take profit|price target)\s*[:=]?\s*\$?\d", re.I)

# A cashtag: a dollar sign followed by LETTERS, which is what Square makes
# clickable. Not a price like $180.
_CASHTAG = re.compile(r"\$[A-Z][A-Z0-9]{1,9}\b")


def _money(value: float) -> str:
    """Prices span nine orders of magnitude on this exchange."""
    if value >= 1000:
        return f"${value:,.0f}"
    if value >= 1:
        return f"${value:,.2f}"
    if value >= 0.01:
        return f"${value:.4f}"
    return f"${value:.8f}".rstrip("0")


def _big(value: float) -> str:
    if value >= 1e9:
        return f"${value/1e9:.1f}B"
    if value >= 1e6:
        return f"${value/1e6:.0f}M"
    return f"${value:,.0f}"


class PostWriter:
    """Turns market numbers into a Square post."""

    MAX_CHARS = 2000

    def __init__(self, ai_engine=None, brand: str = "PressVane"):
        self.ai = ai_engine
        self.brand = brand
        self.last_error = ""

    # ── the facts, stated plainly ────────────────────────────────

    @staticmethod
    def facts(coin: Dict, ctx: Dict) -> str:
        d = "up" if coin["change"] >= 0 else "down"
        pos = coin.get("range_position", 0.5)
        where = ("near the day's high" if pos > 0.8 else
                 "near the day's low" if pos < 0.2 else
                 "mid-range")
        lines = [
            f"{coin['base']} is {d} {abs(coin['change']):.1f}% over 24 hours.",
            f"Price {_money(coin['price'])}, "
            f"range {_money(coin['low'])} to {_money(coin['high'])}, "
            f"closing {where}.",
            f"Volume {_big(coin['volume'])} across {coin['trades']:,} trades.",
        ]
        if ctx:
            lines.append(f"Over 7 days: {ctx['week_change']:+.1f}%. "
                         f"Over 30 days: {ctx['month_change']:+.1f}%.")
            if ctx.get("at_month_high"):
                lines.append("That is at the top of its 30-day range.")
            elif ctx.get("at_month_low"):
                lines.append("That is at the bottom of its 30-day range.")
        return "\n".join(lines)

    # ── the post ─────────────────────────────────────────────────

    async def write(self, coin: Dict, ctx: Dict) -> Optional[Dict]:
        facts = self.facts(coin, ctx)
        body = await self._body(coin, ctx, facts)
        if not body:
            body = self._fallback(coin, ctx)
        # Models leave trailing double-spaces, which Telegram and Square both
        # render as a hard line break in the middle of a paragraph.
        body = "\n".join(l.rstrip() for l in body.splitlines()).strip()

        text = (f"{body.rstrip()}\n\n"
                f"${coin['base']}\n\n"
                f"{DISCLAIMER}")[:self.MAX_CHARS]

        problem = self.check(text)
        if problem:
            logger.warning(f"Post rejected: {problem}. Using the plain version.")
            text = (f"{self._fallback(coin, ctx)}\n\n"
                    f"${coin['base']}\n\n{DISCLAIMER}")
            if self.check(text):
                self.last_error = "even the plain version failed the gate"
                return None

        return {"symbol": coin["symbol"], "base": coin["base"],
                "text": text, "facts": facts, "score": coin["score"],
                "written_at": datetime.now(timezone.utc).isoformat()}

    async def _body(self, coin: Dict, ctx: Dict, facts: str) -> str:
        if not self.ai:
            return ""
        try:
            raw = await self.ai.generate(
                task="social_caption",
                system_prompt=(
                    "You write short market notes for Binance Square. You are "
                    "given the numbers as FACTS -- use them exactly and never "
                    "invent a figure, a date or an event.\n\n"
                    "The RANGE is the day's low and high. It is NOT the open "
                    "and the close, so never write that it 'climbed from' the "
                    "low 'to' the high -- say it traded between them.\n\n"
                    "Write 90-150 words:\n"
                    "1. One line on what moved and by how much.\n"
                    "2. Two or three lines on what the volume and the day's "
                    "range suggest about whether the move is holding.\n"
                    "3. One line on what would confirm it and what would kill "
                    "it, in terms of levels already given.\n"
                    "4. End with a question to the reader.\n\n"
                    "NEVER tell anyone to buy or sell. No price targets, no "
                    "predictions, no hype words, no emoji spam. Plain, calm, "
                    "specific. Output only the post."),
                user_prompt=f"FACTS:\n{facts}\n\nWrite the post.",
                max_tokens=420, temperature=0.7, min_attempts=1)
            return (raw or "").strip()
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.warning(f"Model write failed: {self.last_error}")
            return ""

    def _fallback(self, coin: Dict, ctx: Dict) -> str:
        """
        A publishable post with no model at all.

        The numbers ARE the post. If the model is down the note is drier, but
        it is accurate and it goes out -- which beats missing the day.
        """
        d = "gained" if coin["change"] >= 0 else "lost"
        pos = coin.get("range_position", 0.5)
        holding = ("It is holding near the top of the day's range, which is "
                   "usually the sign of a move with follow-through."
                   if pos > 0.8 else
                   "It has given most of the move back and sits near the day's "
                   "low, which is what a fade looks like."
                   if pos < 0.2 else
                   "It sits mid-range, which is neither confirmation nor "
                   "rejection yet.")
        line = ""
        if ctx:
            if ctx.get("at_month_high"):
                line = " This is the top of its 30-day range."
            elif ctx.get("at_month_low"):
                line = " This is the bottom of its 30-day range."
        return (f"{coin['base']} {d} {abs(coin['change']):.1f}% in 24 hours, "
                f"trading at {_money(coin['price'])}.\n\n"
                f"The move came on {_big(coin['volume'])} of volume across "
                f"{coin['trades']:,} trades, between {_money(coin['low'])} and "
                f"{_money(coin['high'])}.{line}\n\n{holding}\n\n"
                f"What are you watching on this one?")

    # ── the gate ─────────────────────────────────────────────────

    @classmethod
    def check(cls, text: str) -> str:
        """
        Deterministic, never a model. Returns a reason, or "" if it passes.

        Asking a model whether its own market post is responsible is the least
        reliable check available, and the failure mode here is telling
        strangers to buy something.
        """
        if not text or len(text.strip()) < 80:
            return "too short to be worth posting"
        if len(text) > cls.MAX_CHARS:
            return f"over {cls.MAX_CHARS} characters"
        if DISCLAIMER.lower() not in text.lower():
            return "no disclaimer"

        # The disclaimer is removed BEFORE scanning for advice, because it
        # contains the words "financial advice" and the scanner bans them.
        # Every single post rejected itself on the first dry run.
        body = text.lower().replace(DISCLAIMER.lower(), " ")
        m = _ADVICE.search(body)
        if m:
            return f"reads as advice: {m.group(0)!r}"
        m = _TARGET.search(body)
        if m:
            return f"contains a price target: {m.group(0)!r}"
        # A cashtag is a dollar sign followed by LETTERS. Testing for a bare
        # "$" passed on any post that quoted a price -- which is every post,
        # so the check did nothing at all.
        if not _CASHTAG.search(text):
            return "no cashtag, so Square cannot file it on the coin's page"
        if "?" not in text:
            return "no question, so nothing to comment on"
        return ""
