"""
Social syndication — Facebook, X/Twitter, Threads and Bluesky, driven by the
article agent.

Adding another channel is configuration, not code: connect it at buffer.com,
name it in BUFFER_SERVICES, and SERVICE_LIMITS below sizes the caption.
Bluesky is the one worth having beyond the obvious three, because it is the
only one of them that does NOT demote a post for carrying an outbound link
-- and every post here exists to carry one.

MORE THAN ONE BUFFER ACCOUNT
----------------------------
Buffer's free plan caps the channels per account, so the four channels are
split across two logins: the news account carries Facebook, X and Threads,
and Bluesky sits on the account the Pinterest agent already uses, where
there was room.

Each account is searched in turn for the service being posted to, and the
FIRST one holding a live channel wins. Nothing has to be told which login
owns what, so moving a channel between accounts needs no change here.
First-match-wins rather than posting to every match, because a channel
connected on both logins by accident would otherwise publish the story
twice.

A side benefit: rate limits are per account, so splitting the channels
splits the request budget too.

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
Eight articles a day are published (six news, two explainers), and SIX of
them are announced. The volume starts lower still and climbs on its own:

    days 1-10    3 posts/day
    days 11-20   4 posts/day
    day 21+      6 posts/day

The ceiling is six rather than eight deliberately. Six is comfortably
ordinary for a news brand on either platform, and the last two posts of the
day would have been the 11:30 and 13:00 PKT slots -- half past two and four
in the morning in New York, which is where the readers are. Those two
articles lose nothing that matters: they are still written, still published,
still indexed by Google, and search is where the traffic comes from. The
social post is the small half of their value.

The WEBSITE stays at eight. Nothing penalises a news site for publishing
often -- more indexed pages is the whole growth mechanism -- and the risk
being managed here belongs to the social accounts, not to the site.

Day one stamps itself on the first post ever sent and is persisted with the
rest of the brain's state, so the ramp needs no configuration, cannot be
forgotten, and survives a redeploy. SOCIAL_START_DATE overrides it if you
ever need to.

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

    # Volume during the account warm-up: (day threshold, posts allowed).
    # Read in order; the first threshold the account is younger than wins.
    # Past the last one the configured cap applies, which is six.
    # days_live() is zero-based: age 0 is day one. So `age < 10` is the
    # first TEN days, and day eleven is the first to see four.
    RAMP = ((10, 3), (20, 4))

    # How much of the article a Facebook post carries. Long enough to be
    # worth reading on its own -- someone who never clicks should still come
    # away knowing what happened -- and short enough that Facebook does not
    # collapse it behind "See more" before the point is made.
    FACEBOOK_BODY_CHARS = 700

    # Facebook demotes a post that carries an outbound link, and does not
    # demote one whose link sits in the first comment. That is the whole
    # reason for the split: the reach lost by putting the URL in the body is
    # larger than anything else on this page.
    #
    # X and Threads do not get this treatment. Neither supports a first
    # comment through Buffer, and on both a link in the post is normal.
    FACEBOOK_LINK_IN_FIRST_COMMENT = True

    # X counts every link as exactly this many characters, whatever its
    # real length, because it rewrites them through t.co.
    X_LINK_LENGTH = 23
    X_MAX_CHARS = 280
    FACEBOOK_MAX_CHARS = 5000

    # Threads is 500 characters and counts a URL at its REAL length -- there
    # is no t.co equivalent, so a long slug genuinely costs what it reads.
    # Long enough for a proper paragraph, which is why it gets its own
    # caption rather than the Facebook one truncated.
    THREADS_MAX_CHARS = 500

    # Every service Buffer can connect, and what it will accept. Adding a
    # channel at buffer.com and naming it in BUFFER_SERVICES is then the
    # whole job -- no code.
    #
    # This table exists because the fallback used to be the FACEBOOK caption,
    # which runs to 5,000 characters. Connecting Bluesky, whose limit is 300,
    # would have produced a post rejected outright on every article, with
    # nothing but a Buffer error to explain it.
    SERVICE_LIMITS = {
        "twitter":        280,
        "bluesky":        300,
        "threads":        500,
        "mastodon":       500,   # the usual instance default; some allow more
        "pinterest":      500,   # the pin description
        "googlebusiness": 1500,
        "instagram":     2200,
        "tiktok":        2200,
        "linkedin":      3000,
        "facebook":      5000,
    }

    # An unknown service gets the tightest limit in common use rather than
    # the most generous. Too short is a worse post; too long is no post.
    DEFAULT_LIMIT = 300

    # Above this a caption is written long-form -- several paragraphs, the
    # shape Facebook and LinkedIn reward. Below it, one paragraph at most.
    LONG_FORM_ABOVE = 1500

    # Words that make a bad hashtag on their own.
    _TAG_STOP = {"the", "a", "an", "and", "or", "of", "in", "on", "for", "to",
                 "with", "how", "what", "why", "is", "are", "it", "its",
                 "your", "you", "new", "news", "explained"}

    def __init__(self, buffer=None, db=None, growth=None, site_url: str = "",
                 services: Optional[List[str]] = None,
                 caps: Optional[Dict[str, int]] = None,
                 start_date: str = "", brain=None, buffers=None):
        # One account or several. `buffer` stays for the single-account case,
        # which is most callers and every test that predates the split.
        self.buffers = [b for b in (list(buffers) if buffers
                                    else ([buffer] if buffer else [])) if b]
        self.db = db
        self.growth = growth
        self.brain = brain
        self.site_url = (site_url or "").rstrip("/")
        self.services = [s for s in (services or ["facebook", "twitter",
                                                   "threads", "bluesky"]) if s]
        self.caps = {"facebook": 6, "twitter": 6, "threads": 6, "bluesky": 6}
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

    @property
    def buffer(self):
        """The first Buffer account. Callers that assume one still work."""
        return self.buffers[0] if self.buffers else None

    async def find_channels(self, service: str):
        """
        The first Buffer account holding a live channel for this service.

        Returns (broadcaster, channels), or (None, []) if no account has it.
        """
        for buf in self.buffers:
            try:
                if hasattr(buf, "ensure_channels"):
                    found = await buf.ensure_channels(service)
                else:
                    found = buf.channels_for(service)
            except Exception as e:
                logger.warning(f"Buffer account lookup failed for {service}: "
                               f"{type(e).__name__}: {e}")
                continue
            if found:
                return buf, found
        return None, []

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
        """
        Days since the accounts started posting, or None if nothing has yet.

        A configured SOCIAL_START_DATE wins. Otherwise the brain's own stamp
        is used, which it writes the first time anything is posted -- so the
        ramp starts itself rather than waiting on a setting somebody has to
        remember.
        """
        started = self.start_date or self._parse_date(
            getattr(self.brain, "social_started_on", "") or "")
        if not started:
            return None
        return max(0, (self._pkt_now().date() - started).days)

    def daily_cap(self, platform: str) -> int:
        """Today's ceiling for one platform, warm-up included."""
        full = self.caps.get(platform, 0)
        age = self.days_live()
        if age is None:
            return full
        for threshold, allowed in self.RAMP:
            if age < threshold:
                return min(full, allowed)
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

    @classmethod
    def article_excerpt(cls, article: Dict, limit: int) -> str:
        """
        The opening of the article as plain paragraphs.

        A post that is only a headline and a link asks for a click and gives
        nothing back, and neither platform rewards that. This carries enough
        of the story to stand on its own: whole paragraphs, never a fragment,
        so the reader who does not click still learns what happened and the
        one who does knows why it is worth the trip.
        """
        html = article.get("content") or ""
        if not html:
            return ""

        # Paragraphs only. Headings are signposts for a page, not for a feed.
        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", html,
                                flags=re.S | re.I)
        out, used = [], 0
        for raw in paragraphs:
            text = re.sub(r"<[^>]+>", "", raw)
            text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                        .replace("&lt;", "<").replace("&gt;", ">")
                        .replace("&#39;", "'").replace("&quot;", '"'))
            text = " ".join(text.split())
            if len(text) < 40:            # a caption or a photo credit
                continue
            if used and used + len(text) > limit:
                break
            out.append(text)
            used += len(text) + 2
            if used >= limit:
                break

        if not out:
            return ""
        # One paragraph that alone overruns the budget is cut to a sentence.
        if len(out) == 1 and len(out[0]) > limit:
            return cls._trim(out[0], limit)
        return "\n\n".join(out)

    def facebook_caption(self, article: Dict, link: str,
                         link_in_body: bool = True) -> str:
        """
        Built from the article itself, not rewritten by a model.

        The article was already written and already passed the quality gate.
        Paying a model to paraphrase it adds latency, cost and one more way
        to publish nonsense, for nothing.

        The opening of the piece is carried in full so the post is worth
        reading on its own. `link_in_body` decides whether the URL goes here
        or into the first comment -- see facebook_first_comment.
        """
        title = (article.get("title") or "").strip()
        body = self.article_excerpt(article, self.FACEBOOK_BODY_CHARS)
        if not body:
            # No body to quote (an older record, or an odd one): the written
            # summary is the next best thing.
            body = self._blurb(article, 300)
        tags = self.hashtags(article.get("seo_keywords"), 3)

        parts = [p for p in (title, body) if p]
        if link_in_body:
            parts.append(f"📖 Read the full story: {link}")
        if tags:
            parts.append(" ".join(tags))
        return "\n\n".join(parts)[:self.FACEBOOK_MAX_CHARS]

    def facebook_first_comment(self, link: str) -> str:
        """
        The comment posted under a Facebook post, carrying the link.

        Kept to one line. A comment is read at a glance or not at all, and
        anything longer starts competing with the post above it.
        """
        return f"📖 Read the full story: {link}" if link else ""

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

    def limit_for(self, service: str) -> int:
        """How many characters this service will accept."""
        return self.SERVICE_LIMITS.get(service, self.DEFAULT_LIMIT)

    def link_cost(self, service: str, link: str) -> int:
        """
        What the link costs against the limit.

        X is the only one that rewrites URLs, through t.co, at a flat 23
        characters however long the real one is. Everywhere else a long slug
        genuinely costs what it reads, and budgeting it at 23 overruns.
        """
        return self.X_LINK_LENGTH if service == "twitter" else len(link)

    def sized_caption(self, service: str, article: Dict, link: str) -> str:
        """
        A caption built to fit one service, whatever its limit.

        The body is sized to what is left AFTER the link and the hashtags,
        rather than written and then cut, so the post always ends on a whole
        sentence instead of stopping mid-word.
        """
        limit = self.limit_for(service)
        title = " ".join((article.get("title") or "").split())
        tags = self.hashtags(article.get("seo_keywords"),
                             3 if limit >= self.LONG_FORM_ABOVE else 2)
        tag_line = " ".join(tags)

        overhead = self.link_cost(service, link) + 2
        if tag_line:
            overhead += len(tag_line) + 2
        budget = limit - overhead

        if len(title) > budget:
            # Nothing but the headline will fit, and even that has to be cut.
            title = self._trim(title, max(1, budget))
            body = ""
        else:
            spare = budget - len(title) - 2
            room = min(spare, self.FACEBOOK_BODY_CHARS) \
                if limit >= self.LONG_FORM_ABOVE else spare
            body = self.article_excerpt(article, room) if room >= 80 else ""
            if not body and room >= 80:
                body = self._blurb(article, room)

        parts = [p for p in (title, body) if p]
        parts.append(link)
        if tag_line:
            parts.append(tag_line)
        text = "\n\n".join(parts)

        measured = len(text) - len(link) + self.link_cost(service, link) \
            if link in text else len(text)
        if measured > limit:            # belt and braces
            logger.warning(f"{service} caption overran its {limit}-character "
                           f"budget; falling back to the headline and link.")
            head = self._trim(title, max(1, limit - self.link_cost(service, link) - 3))
            text = f"{head}\n\n{link}"
        return text

    def threads_caption(self, article: Dict, link: str) -> str:
        """Threads: 500 characters, link charged at its real length."""
        return self.sized_caption("threads", article, link)

    def caption_for(self, service: str, article: Dict, link: str) -> str:
        """
        The post text for one service.

        Facebook and X have their own builders because each has a rule
        nothing else shares: Facebook can put the link in a comment, and X
        prices every URL at a flat 23 characters. Every other service --
        Threads today, Bluesky or LinkedIn or Mastodon the day one is
        connected -- is sized from SERVICE_LIMITS with no code to write.
        """
        if service == "twitter":
            return self.x_caption(article, link)
        if service == "facebook":
            return self.facebook_caption(
                article, link,
                link_in_body=not self.FACEBOOK_LINK_IN_FIRST_COMMENT)
        return self.sized_caption(service, article, link)

    def first_comment_for(self, service: str, link: str) -> str:
        """The comment to post underneath, if the service supports one."""
        if service == "facebook" and self.FACEBOOK_LINK_IN_FIRST_COMMENT:
            return self.facebook_first_comment(link)
        return ""

    # ── publishing ───────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        return bool(self.buffers and self.site_url and self.services)

    @property
    def is_on(self) -> bool:
        """The master switch. Without a brain wired in, nothing is gated."""
        if self.brain is None:
            return True
        return bool(getattr(self.brain, "social_module_active", False)
                    and not getattr(self.brain, "master_kill", False))

    def platform_is_on(self, service: str) -> bool:
        if self.brain is None:
            return True
        checker = getattr(self.brain, "social_enabled", None)
        return bool(checker(service)) if checker else True

    async def syndicate(self, article: Optional[Dict]) -> Dict[str, bool]:
        """
        Announces one published article everywhere it belongs.

        Called the moment the article is live. Never raises: a social network
        being unreachable is not a reason to disturb a page that has already
        published, and one platform failing never touches the other.
        """
        results: Dict[str, bool] = {}
        if not (article and self.buffers):
            return results

        if not self.is_on:
            logger.info("Social module is OFF — the article is published, "
                        "but not announced.")
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
        if not self.platform_is_on(service):
            logger.info(f"{service} is switched off — skipping.")
            return False

        # Searched across every Buffer account, and through ensure_channels
        # rather than channels_for: a channel connected at buffer.com after
        # this process started is otherwise invisible until a deploy.
        buf, channels = await self.find_channels(service)
        if not channels:
            logger.info(f"No connected {service} channel on any Buffer "
                        f"account — skipping. Connect it at buffer.com and "
                        f"it is picked up automatically.")
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
        comment = self.first_comment_for(service, link)
        slug = article.get("slug", "")

        # The link has to reach the reader by one route or the other. If it
        # is in neither the post nor a comment, the post is pointless and is
        # not worth sending at all.
        if link not in text and link not in comment:
            logger.error(f"{service}: the post would carry no link. Skipping.")
            return False

        any_ok = False
        for channel in channels:
            ok = await buf.send(channel, text, image_url=image,
                                article_slug=slug,
                                first_comment=comment)
            any_ok = any_ok or ok
            await asyncio.sleep(1)          # be gentle with the API

        if any_ok:
            self.sent_today[service] = self.sent_today.get(service, 0) + 1
            # Day one of the warm-up is the first post that actually left.
            if self.brain is not None and hasattr(self.brain, "note_social_start"):
                try:
                    self.brain.note_social_start()
                except Exception:
                    pass
            if self.growth:
                try:
                    self.growth.record_action(self.growth.ACTION_POST,
                                              {"platform": service})
                except Exception:
                    pass
        else:
            self.last_error = getattr(buf, "last_error", "") or "post rejected"
        return any_ok

    @property
    def status(self) -> Dict:
        self._roll_day()
        return {
            "ready": self.is_ready,
            "active": self.is_on,
            "buffer_accounts": [getattr(b, "account_email", "") or "?"
                                for b in self.buffers],
            "platforms_on": {s: self.platform_is_on(s) for s in self.services},
            "services": list(self.services),
            "site_url": self.site_url,
            "days_live": self.days_live(),
            "caps_today": {s: self.daily_cap(s) for s in self.services},
            "sent_today": dict(self.sent_today),
            "skipped_today": dict(self.skipped_today),
            "last_error": self.last_error,
        }
