"""
Social syndication — Facebook and X/Twitter, driven by the article agent.

Every published article is announced on both, at the moment it goes live,
always with a link back to the site. That link is the entire point: these
accounts are not the product, they are a road to the product. A post that
cannot carry a link is not sent at all.

WHY THIS IS NOT PART OF THE TELEGRAM FAN-OUT
--------------------------------------------
It used to be. Facebook hung off `Fanout.distribute()`, which runs on the
Telegram schedule, and that stopped making sense the day the website was
decoupled: the Telegram slots and the article slots now carry DIFFERENT
stories, so a Facebook post fired from the Telegram side had no article to
link to and went out bare — the one thing it must never do. Worse, when the
two schedules did collide the same story went to Facebook twice.

So the trigger is the article, not the channel. Telegram is untouched by
anything in this file.

POSTING VOLUME
--------------
Eight articles a day are published (six news, two explainers) and every one
of them is worth announcing — the writing is already paid for, and skipping
the share is pure loss.

Brand-new accounts are the exception. An account whose first day of life
includes eight outbound links reads as a link farm to both platforms, and
the reach penalty for that is the kind you do not recover from. So when
SOCIAL_START_DATE is set the volume ramps:

    days 1-7    4 posts/day
    days 8-14   6 posts/day
    day 15+     8 posts/day  (every article)

The articles that survive the cap are chosen by AUDIENCE, not by whichever
happened to publish first. Ranked best-first below; a cap of four keeps the
top four. Picking "the first four of the day" instead would hand every share
to the small hours, because the day starts at midnight in Pakistan while the
readers are in London and New York.
"""
import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("OmniBot.Social")

PKT_OFFSET = timedelta(hours=5)


class SocialSyndicator:
    """Announces each published article on Facebook and X."""

    # Publishing slots ranked by how many people are awake to read them.
    # Every entry must exist in BotBrain.SCHEDULE (article_slots +
    # evergreen_slots); a test enforces that, because a schedule change here
    # without one there silently mutes a platform for that slot.
    #
    #    PKT      UTC     London   New York
    SLOT_PRIORITY = [
        (18, 0),    # 13:00   14:00   09:00   US peak, UK afternoon
        (21, 0),    # 16:00   17:00   12:00   US lunch
        (1, 0),     # 20:00   21:00   16:00   US afternoon
        (19, 30),   # 14:30   15:30   10:30   US morning      (explainer)
        (4, 0),     # 23:00   00:00   19:00   US evening
        (15, 0),    # 10:00   11:00   06:00   US wakes
        (11, 30),   # 06:30   07:30   02:30   UK commute
        (13, 0),    # 08:00   09:00   04:00   UK morning      (explainer)
    ]

    # How far from a scheduled slot a publish can land and still count as
    # that slot. Covers the 30-minute deferral an unillustrated story takes.
    NEAR_SLOT_MINUTES = 120

    # Volume during the account warm-up, by week.
    RAMP = (4, 6)

    # X counts every link as exactly this many characters, whatever its
    # real length, because it rewrites them through t.co.
    X_LINK_LENGTH = 23
    X_MAX_CHARS = 280
    FACEBOOK_MAX_CHARS = 5000

    # Words that make a bad hashtag on their own.
    _TAG_STOP = {"the", "a", "an", "and", "or", "of", "in", "on", "for", "to",
                 "with", "how", "what", "why", "is", "are", "it", "its",
                 "your", "you", "new", "news", "explained"}

    def __init__(self, buffer=None, db=None, growth=None, site_url: str = "",
                 services: Optional[List[str]] = None,
                 caps: Optional[Dict[str, int]] = None,
                 start_date: str = ""):
        self.buffer = buffer
        self.db = db
        self.growth = growth
        self.site_url = (site_url or "").rstrip("/")
        self.services = [s for s in (services or ["facebook", "twitter"]) if s]
        self.caps = {"facebook": 8, "twitter": 8}
        self.caps.update(caps or {})
        self.start_date = self._parse_date(start_date)

        self.sent_today: Dict[str, int] = {s: 0 for s in self.services}
        self.skipped_today: Dict[str, int] = {s: 0 for s in self.services}
        self._counter_day: Optional[date] = None
        self.last_error = ""

        if not self.site_url:
            logger.warning("SITE_URL is empty — social syndication is disabled. "
                           "A post without a link back to the article is worse "
                           "than no post at all.")
        elif self.start_date is None:
            logger.info(f"Social syndication live at the full cap "
                        f"({self.caps}). Set SOCIAL_START_DATE to ramp a new "
                        f"account up gradually instead.")

    # ── time and volume ──────────────────────────────────────────

    @staticmethod
    def _parse_date(value: str) -> Optional[date]:
        raw = (value or "").strip()
        if not raw:
            return None
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except ValueError:
            logger.warning(f"SOCIAL_START_DATE='{raw}' is not YYYY-MM-DD. "
                           f"Ignoring it and using the full cap.")
            return None

    @staticmethod
    def _pkt_now() -> datetime:
        return datetime.now(timezone.utc) + PKT_OFFSET

    def days_live(self) -> Optional[int]:
        """Days since the accounts opened, or None if that was never set."""
        if not self.start_date:
            return None
        return max(0, (self._pkt_now().date() - self.start_date).days)

    def daily_cap(self, platform: str) -> int:
        """Today's ceiling for one platform, warm-up included."""
        full = self.caps.get(platform, 0)
        age = self.days_live()
        if age is None:
            return full
        if age < 7:
            return min(full, self.RAMP[0])
        if age < 14:
            return min(full, self.RAMP[1])
        return full

    def _roll_day(self) -> None:
        """Clears the counters when the PKT date changes. Self-healing, so a
        restart or a missed midnight tick cannot leave a stale count in
        place and mute a platform for the rest of the day."""
        today = self._pkt_now().date()
        if self._counter_day != today:
            if self._counter_day is not None:
                logger.info(f"Social counters reset for {today}. "
                            f"Yesterday: {dict(self.sent_today)}")
            self._counter_day = today
            self.sent_today = {s: 0 for s in self.services}
            self.skipped_today = {s: 0 for s in self.services}

    def _nearest_slot(self, when: datetime):
        """The scheduled slot this publish belongs to, and how far off it is."""
        now_m = when.hour * 60 + when.minute
        best, best_gap = None, 10 ** 9
        for hour, minute in self.SLOT_PRIORITY:
            gap = abs(now_m - (hour * 60 + minute))
            gap = min(gap, 1440 - gap)          # the day wraps at midnight
            if gap < best_gap:
                best, best_gap = (hour, minute), gap
        return best, best_gap

    def _slot_is_allowed(self, platform: str, when: datetime) -> bool:
        """Whether this platform shares the article publishing at `when`."""
        cap = self.daily_cap(platform)
        if cap >= len(self.SLOT_PRIORITY):
            return True
        slot, gap = self._nearest_slot(when)
        if gap > self.NEAR_SLOT_MINUTES:
            # Published off-schedule — a manual run, or a story that came back
            # from several deferrals. The daily counter is the only gate here;
            # there is no slot to rank it against.
            return True
        return slot in self.SLOT_PRIORITY[:cap]

    # ── text ─────────────────────────────────────────────────────

    def article_link(self, slug: str) -> str:
        """The canonical URL. The site serves articles at /{slug}."""
        if not (slug and self.site_url):
            return ""
        return f"{self.site_url}/{slug.strip('/')}"

    # A word, keeping any hyphen or apostrophe inside it. Splitting on the
    # hyphen instead turned "on-chain analysis" into #ChainAnalysis, because
    # "on" then looked like a stop word standing on its own.
    _WORD = re.compile(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)*")

    # Dashes the AI writes that are not the ASCII one.
    _DASHES = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-",
                            "\u2013": "-", "\u2014": "-", "\u2212": "-"})

    MAX_TAG_LENGTH = 28

    @classmethod
    def _hashtag(cls, keyword: str) -> str:
        """Turns an SEO keyword into a usable hashtag, or returns ''."""
        words = cls._WORD.findall((keyword or "").translate(cls._DASHES))

        # Stop words are trimmed from the ENDS only. Removing them from the
        # middle rewrites the term: "the price of bitcoin" is meant to become
        # #PriceOfBitcoin, not #PriceBitcoin.
        while words and words[0].lower() in cls._TAG_STOP:
            words.pop(0)
        while words and words[-1].lower() in cls._TAG_STOP:
            words.pop()

        # Four words or more is a sentence, not a hashtag.
        if not words or len(words) > 3:
            return ""

        parts = [piece[0].upper() + piece[1:]
                 for word in words
                 for piece in re.split(r"['-]", word) if piece]
        tag = "".join(parts)
        if len(tag) < 3 or len(tag) > cls.MAX_TAG_LENGTH or tag.isdigit():
            return ""
        return f"#{tag}"

    @classmethod
    def hashtags(cls, keywords, limit: int) -> List[str]:
        out, seen = [], set()
        for kw in (keywords or []):
            tag = cls._hashtag(kw if isinstance(kw, str) else "")
            if tag and tag.lower() not in seen:
                seen.add(tag.lower())
                out.append(tag)
            if len(out) >= limit:
                break
        return out

    # A sentence shorter than this is a stub, not a summary. Judged in
    # characters rather than as a fraction of the limit: a fraction meant a
    # generous budget REJECTED a perfectly good 78-character opening sentence
    # and the post lost its context entirely.
    MIN_SENTENCE = 60

    @classmethod
    def _trim(cls, text: str, limit: int) -> str:
        """Cuts to `limit` on a sentence boundary, then a word boundary."""
        text = " ".join((text or "").split())
        if len(text) <= limit:
            return text
        window = text[:limit + 1]
        floor = min(limit, cls.MIN_SENTENCE)
        for stop in (". ", "! ", "? "):
            cut = window.rfind(stop)
            if cut + 1 >= floor:
                return window[:cut + 1].strip()
        cut = window.rfind(" ")
        return (window[:cut] if cut > 0 else window[:limit]).rstrip(" ,;:—-") + "…"

    def _blurb(self, article: Dict, limit: int) -> str:
        """
        A line of context under the headline — or nothing at all.

        `summary` comes first and `meta_description` second, which is the
        opposite of what you would guess. The meta description is cut to 160
        characters for the search snippet and in practice almost always stops
        mid-clause ("...and token age-spent, helping"). That is invisible in a
        search result and glaring in a Facebook post.

        Anything that does not end as a finished sentence is dropped rather
        than published, so a post is never a database field that ran out of
        room.
        """
        for candidate in (article.get("summary"), article.get("meta_description")):
            text = self._trim(candidate or "", limit)
            if text and text.endswith((".", "!", "?", "\u201d", '"')):
                return text
        return ""

    def facebook_caption(self, article: Dict, link: str) -> str:
        """
        Built from the article itself, not rewritten by a model.

        The headline and meta description were already written and already
        passed the quality gate. Paying a model to paraphrase them adds
        latency, cost and one more way to publish nonsense, for nothing.
        """
        title = (article.get("title") or "").strip()
        blurb = self._blurb(article, 300)
        tags = self.hashtags(article.get("seo_keywords"), 3)

        parts = [p for p in (title, blurb) if p]
        parts.append(f"📖 Read the full story: {link}")
        if tags:
            parts.append(" ".join(tags))
        return "\n\n".join(parts)[:self.FACEBOOK_MAX_CHARS]

    def x_length(self, text: str, link: str) -> int:
        """The length X will actually count, with the link priced at 23."""
        if link and link in text:
            return len(text) - len(link) + self.X_LINK_LENGTH
        return len(text)

    def x_caption(self, article: Dict, link: str) -> str:
        """
        280 characters, hard. Buffer rejects anything longer outright, so this
        measures rather than hopes: the link is priced at t.co's fixed 23
        characters, and the headline is trimmed to whatever is left.
        """
        title = " ".join((article.get("title") or "").split())
        tags = self.hashtags(article.get("seo_keywords"), 2)
        tag_line = " ".join(tags)

        # Everything except the headline is fixed cost: the link, the blank
        # line before it, and the hashtags on their own line.
        overhead = self.X_LINK_LENGTH + 2
        if tag_line:
            overhead += len(tag_line) + 2
        budget = self.X_MAX_CHARS - overhead

        if budget < 20:            # pathological keywords; drop them instead
            tag_line = ""
            budget = self.X_MAX_CHARS - self.X_LINK_LENGTH - 2

        headline = title if len(title) <= budget else self._trim(title, budget - 1)

        # A blurb only earns its place if a whole sentence of it fits.
        spare = budget - len(headline) - 2
        if spare >= 60:
            blurb = self._blurb(article, spare)
            if blurb:
                headline = f"{headline}\n{blurb}"

        text = f"{headline}\n\n{link}"
        if tag_line:
            text = f"{text}\n\n{tag_line}"

        if self.x_length(text, link) > self.X_MAX_CHARS:   # belt and braces
            logger.warning("X caption overran its budget; falling back to "
                           "headline and link only.")
            text = f"{self._trim(title, self.X_MAX_CHARS - self.X_LINK_LENGTH - 3)}\n\n{link}"
        return text

    def caption_for(self, service: str, article: Dict, link: str) -> str:
        if service == "twitter":
            return self.x_caption(article, link)
        return self.facebook_caption(article, link)

    # ── publishing ───────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        return bool(self.buffer and self.site_url and self.services)

    async def syndicate(self, article: Optional[Dict]) -> Dict[str, bool]:
        """
        Announces one published article everywhere it belongs.

        Called the moment the article is live. Never raises: a social network
        being unreachable is not a reason to disturb a page that has already
        published, and one platform failing never touches the other.
        """
        results: Dict[str, bool] = {}
        if not (article and self.buffer):
            return results

        link = self.article_link(article.get("slug", ""))
        if not link:
            logger.warning("No article link could be built (SITE_URL unset?) — "
                           "not posting. Every post has to carry the link.")
            return results

        self._roll_day()
        image = article.get("main_image_url") or ""

        for service in self.services:
            try:
                results[service] = await self._to_service(service, article, link, image)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                logger.error(f"{service} syndication raised {self.last_error}")
                results[service] = False
                if self.db:
                    await self.db.log_error("SocialSyndicator", type(e).__name__,
                                            str(e), auto_resolved=True)

        delivered = [k for k, v in results.items() if v]
        logger.info(f"Article /{article.get('slug')} announced on: "
                    f"{', '.join(delivered) or 'nothing'}")
        return results

    async def _to_service(self, service: str, article: Dict,
                          link: str, image: str) -> bool:
        # ensure_channels, not channels_for: a page connected at buffer.com
        # after this process started is otherwise invisible until a deploy.
        if hasattr(self.buffer, "ensure_channels"):
            channels = await self.buffer.ensure_channels(service)
        else:
            channels = self.buffer.channels_for(service)
        if not channels:
            logger.info(f"No connected {service} channel in Buffer — skipping. "
                        f"Connect it at buffer.com and it is picked up "
                        f"automatically.")
            return False

        cap = self.daily_cap(service)
        if self.sent_today.get(service, 0) >= cap:
            self.skipped_today[service] = self.skipped_today.get(service, 0) + 1
            logger.info(f"{service}: daily cap reached "
                        f"({self.sent_today[service]}/{cap}).")
            return False

        now = self._pkt_now()
        if not self._slot_is_allowed(service, now):
            self.skipped_today[service] = self.skipped_today.get(service, 0) + 1
            slot, _ = self._nearest_slot(now)
            logger.info(f"{service}: the {slot[0]:02d}:{slot[1]:02d} PKT slot is "
                        f"outside today's top {cap} — not shared while the "
                        f"account is still warming up.")
            return False

        text = self.caption_for(service, article, link)
        slug = article.get("slug", "")

        any_ok = False
        for channel in channels:
            ok = await self.buffer.send(channel, text, image_url=image,
                                        article_slug=slug)
            any_ok = any_ok or ok
            await asyncio.sleep(1)          # be gentle with the API

        if any_ok:
            self.sent_today[service] = self.sent_today.get(service, 0) + 1
            if self.growth:
                try:
                    self.growth.record_action(self.growth.ACTION_POST,
                                              {"platform": service})
                except Exception:
                    pass
        else:
            self.last_error = getattr(self.buffer, "last_error", "") or "post rejected"
        return any_ok

    @property
    def status(self) -> Dict:
        self._roll_day()
        return {
            "ready": self.is_ready,
            "services": list(self.services),
            "site_url": self.site_url,
            "days_live": self.days_live(),
            "caps_today": {s: self.daily_cap(s) for s in self.services},
            "sent_today": dict(self.sent_today),
            "skipped_today": dict(self.skipped_today),
            "last_error": self.last_error,
        }
