"""
Image Generator.

Pictures come from Bing Image Creator (DALL-E 3) and nowhere else — no other
generator is used. When Bing cannot deliver, the chain falls back to real
photography rather than to a different AI:

    1. Bing Image Creator  — DALL-E 3, needs a live `_U` cookie
    2. The photo published with the original news story
    3. A branded headline card drawn locally

Tier 3 needs no network and no installed fonts, so `generate()` returning
None means the disk write itself failed — nothing else can produce it.

The cookie expires every few weeks. When it does, Bing stops redirecting and
the failure is silent, so a dead cookie raises an email alert asking for a
replacement instead of quietly degrading every post.

Everything here is async. The original version was synchronous and called
from inside the event loop, so a slow Bing request froze the whole bot —
scheduler, Telegram keepalives and the dashboard API alike.
"""
import asyncio
import io
import logging
import os
import random
import re
import time
import urllib.parse
from datetime import datetime
from typing import Optional, Tuple

import httpx
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("OmniBot.ImageGen")

# ── Bing Image Creator ────────────────────────────────────────────────
BING_URL = "https://www.bing.com"
BING_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/x-www-form-urlencoded",
    "referrer": "https://www.bing.com/images/create/",
    "origin": "https://www.bing.com",
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/110.0.0.0 Safari/537.36 Edg/110.0.1587.63"
    ),
}
BING_JUNK = {
    "https://r.bing.com/rp/in-2zU3AJUdkgFe7ZKv19yPBHVs.png",
    "https://r.bing.com/rp/TX9QuO3WzcCJz1uaaSwQAz39Kb0.jpg",
}

# Bing serves the create page (HTTP 200) instead of redirecting when the
# cookie is no longer valid, so these are how a dead cookie announces itself.
BING_SIGNIN_MARKERS = ("sign in", "signin", "login.live.com", "rewardsstatus")

# ── Look per category ─────────────────────────────────────────────────
COLOR_PALETTES = {
    "tech_ai": {"start": (15, 23, 42), "end": (59, 130, 246), "accent": (139, 92, 246)},
    "tech": {"start": (15, 23, 42), "end": (59, 130, 246), "accent": (139, 92, 246)},
    "business_markets": {"start": (6, 78, 59), "end": (16, 185, 129), "accent": (245, 158, 11)},
    "business": {"start": (6, 78, 59), "end": (16, 185, 129), "accent": (245, 158, 11)},
    "world_news": {"start": (69, 26, 26), "end": (185, 60, 60), "accent": (251, 191, 36)},
    "politics": {"start": (30, 20, 60), "end": (100, 40, 120), "accent": (220, 180, 60)},
    "sports": {"start": (10, 50, 30), "end": (20, 140, 70), "accent": (255, 200, 50)},
    "crypto": {"start": (20, 10, 40), "end": (120, 60, 200), "accent": (255, 180, 50)},
    "pakistan": {"start": (0, 50, 26), "end": (0, 120, 56), "accent": (240, 240, 240)},
    "morning_brief": {"start": (18, 22, 40), "end": (70, 90, 150), "accent": (255, 200, 90)},
    "default": {"start": (26, 28, 46), "end": (74, 78, 120), "accent": (100, 200, 255)},
}

CATEGORY_LABELS = {
    "tech_ai": "TECH / AI",
    "tech": "TECHNOLOGY",
    "business_markets": "BUSINESS",
    "business": "BUSINESS",
    "world_news": "WORLD",
    "politics": "POLITICS",
    "sports": "SPORT",
    "crypto": "CRYPTO",
    "pakistan": "PAKISTAN",
    "morning_brief": "MORNING BRIEF",
    "default": "NEWS",
}

STYLE_PREFIXES = {
    "tech_ai": "Cyberpunk server room, neon lights, glowing futuristic hardware",
    "tech": "Modern technology close-up, clean industrial design, studio lighting",
    "business_markets": "Glass skyscraper financial district, trading floor, stock chart aesthetic",
    "business": "Glass skyscraper financial district, trading floor, stock chart aesthetic",
    "world_news": "International summit hall, world map hologram, press briefing room",
    "politics": "Parliament chamber, podium and flags, serious political photojournalism",
    "sports": "Floodlit stadium, dramatic action photography, packed crowd",
    "crypto": "Golden physical cryptocurrency coins, blockchain network visualisation, dark moody lighting",
    "pakistan": "South Asian metropolitan skyline, Islamabad and Karachi streets, documentary photojournalism",
}
DEFAULT_STYLE = "Ultra high quality cinematic photojournalism photograph"

# Fonts, best first. The final fallback is Pillow's bundled face, which is
# always present — so the branded card renders even on a bare container.
FONT_CANDIDATES_BOLD = [
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "arialbd.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
]
FONT_CANDIDATES_REGULAR = [
    "DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "arial.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
]


class ImageGenerator:
    """Produces a 1280x720 JPEG header image for a post. Never gives up early."""

    WIDTH = 1280
    HEIGHT = 720
    JPEG_QUALITY = 88

    # Bing's own polling loop can legitimately take over a minute, so its
    # ceiling is generous; the point is only that it cannot run unbounded.
    BING_TIMEOUT = 110.0
    BING_ATTEMPTS = 2
    STORY_IMAGE_TIMEOUT = 25.0

    # How many consecutive Bing failures before we conclude the cookie is dead
    # rather than the request being unlucky, and how long before we say so again.
    COOKIE_ALERT_AFTER = 3
    COOKIE_ALERT_INTERVAL = 6 * 3600

    def __init__(self, output_dir: str, channel_name: str = "Novi News",
                 bing_cookie: str = "", total_budget: float = 300.0,
                 notification_manager=None, db=None):
        self.output_dir = output_dir
        self.channel_name = channel_name
        self.bing_cookie = (bing_cookie or "").strip()
        self.total_budget = total_budget
        self.nm = notification_manager
        self.db = db
        self.width = self.WIDTH
        self.height = self.HEIGHT

        # Counters the dashboard reports, so a silent slide onto the
        # last-resort card is visible instead of being discovered in the feed.
        self.stats = {"bing": 0, "story_image": 0, "card": 0, "failed": 0}
        self.last_source = ""

        # Cookie health
        self.consecutive_bing_failures = 0
        self.cookie_looks_dead = False
        self._last_cookie_alert = 0.0

        os.makedirs(output_dir, exist_ok=True)
        logger.info(f"Image Generator ready. Output: {output_dir} | "
                    f"Bing cookie: {'present' if self.bing_cookie else 'ABSENT'}")
        if not self.bing_cookie:
            logger.error("BING_COOKIE is not set — every post will fall back to the "
                         "news photo or a headline card.")

    # ── public API ────────────────────────────────────────────────────

    async def generate(self, headline: str, category: str = "default",
                       source_credit: str = "", story_image_url: str = "") -> Optional[str]:
        """
        Returns the path to a saved JPEG, or None only if the disk write failed.

        `story_image_url` is the photo from the original article, used as a
        tier before falling back to a drawn card.
        """
        headline = (headline or "Breaking News").strip()
        deadline = time.monotonic() + self.total_budget
        prompt = self._build_prompt(headline, category)

        def left() -> float:
            return deadline - time.monotonic()

        img: Optional[Image.Image] = None
        tier = "card"

        # Bing is the only generator used. It is retried, because it is the
        # only thing standing between the post and a fallback photograph.
        if self.bing_cookie:
            for attempt in range(1, self.BING_ATTEMPTS + 1):
                remaining = left()
                if remaining < 15:
                    logger.warning("Image budget exhausted before Bing could retry.")
                    break
                try:
                    img = await asyncio.wait_for(
                        self._from_bing(prompt),
                        timeout=min(self.BING_TIMEOUT, remaining),
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"Bing timed out (attempt {attempt}/{self.BING_ATTEMPTS}).")
                    img = None
                except Exception as e:
                    logger.warning(f"Bing failed (attempt {attempt}/{self.BING_ATTEMPTS}): "
                                   f"{type(e).__name__}: {e}")
                    img = None

                if img is not None:
                    tier = "bing"
                    await self._note_bing_success()
                    logger.info(f"Image generated by Bing DALL-E 3 (attempt {attempt}).")
                    break
                if attempt < self.BING_ATTEMPTS:
                    await asyncio.sleep(3)

            if img is None:
                await self._note_bing_failure(headline)

        # Fall back to the photograph the outlet published with the story.
        # Real reporting imagery beats a synthetic stand-in.
        if img is None and story_image_url and left() > 5:
            try:
                img = await asyncio.wait_for(
                    self._from_url(story_image_url),
                    timeout=min(self.STORY_IMAGE_TIMEOUT, left()),
                )
            except Exception as e:
                logger.warning(f"News photo unusable: {type(e).__name__}: {e}")
                img = None
            if img is not None:
                tier = "story_image"
                logger.info("Using the photo published with the original news story.")

        if img is None:
            logger.warning("Bing unavailable and no usable news photo — "
                           "drawing branded headline card.")
            img = self._branded_card(headline, category, source_credit)

        path = self._save(img)
        if path:
            self.stats[tier] = self.stats.get(tier, 0) + 1
            self.last_source = tier
        else:
            self.stats["failed"] += 1
            self.last_source = "failed"
        return path

    # ── cookie health ─────────────────────────────────────────────────

    async def _note_bing_success(self):
        if self.cookie_looks_dead:
            logger.info("Bing is answering again — the cookie is live.")
            if self.nm:
                await self.nm.send_notification(
                    subject="Bing image cookie is working again",
                    message="Bing Image Creator started responding again. "
                            "Posts are back on DALL-E 3 images.",
                    is_critical=False,
                )
        self.consecutive_bing_failures = 0
        self.cookie_looks_dead = False

    async def _note_bing_failure(self, headline: str = ""):
        """
        Counts a failed Bing run and, once it is clearly not bad luck, emails
        asking for a fresh cookie.

        A single failure is not evidence — Bing throttles and times out. Several
        in a row, however, is what an expired `_U` cookie looks like from here,
        and it otherwise degrades every post silently.
        """
        self.consecutive_bing_failures += 1
        if self.consecutive_bing_failures < self.COOKIE_ALERT_AFTER:
            return

        self.cookie_looks_dead = True
        now = time.time()
        if now - self._last_cookie_alert < self.COOKIE_ALERT_INTERVAL:
            return          # already asked recently; don't nag every post
        self._last_cookie_alert = now

        detail = (
            f"Bing Image Creator has failed {self.consecutive_bing_failures} times "
            f"in a row, which is what an expired cookie looks like.\n\n"
            f"Posts are still going out — they now use the photo from the original "
            f"news story, or a branded headline card — but they are no longer "
            f"DALL-E 3 images.\n\n"
            f"To fix it:\n"
            f"  1. Open https://www.bing.com/images/create in a signed-in browser\n"
            f"  2. Open DevTools > Application > Cookies > https://www.bing.com\n"
            f"  3. Copy the value of the cookie named  _U\n"
            f"  4. Paste it into BING_COOKIE in Render > Environment, and redeploy\n\n"
            f"Last headline attempted: {headline[:120] or 'n/a'}"
        )
        logger.error("BING COOKIE LOOKS EXPIRED — alerting.")

        if self.db:
            await self.db.log_error(
                module="ImageGenerator",
                error_type="BingCookieExpired",
                error_message=f"{self.consecutive_bing_failures} consecutive Bing failures",
                auto_resolved=False,
            )
        if self.nm:
            await self.nm.send_notification(
                subject="Action needed: Bing image cookie has expired",
                message=detail,
                is_critical=True,
            )
        else:
            logger.error("No notification manager wired — cannot email the cookie alert.")

    # ── tier 1: Bing Image Creator ────────────────────────────────────

    async def _from_bing(self, prompt: str) -> Optional[Image.Image]:
        """DALL-E 3 via the Image Creator web flow. Needs a live `_U` cookie."""
        encoded = urllib.parse.quote(prompt)
        headers = dict(BING_HEADERS)
        # Rotated per call: a fixed value across every request is itself a signal.
        headers["x-forwarded-for"] = (
            f"13.{random.randint(104, 107)}.{random.randint(0, 255)}.{random.randint(0, 255)}"
        )

        async with httpx.AsyncClient(headers=headers, cookies={"_U": self.bing_cookie},
                                     timeout=httpx.Timeout(30.0),
                                     follow_redirects=False) as client:
            payload = f"q={encoded}&qs=ds"
            response = None
            for rt in ("3", "4"):
                response = await client.post(
                    f"{BING_URL}/images/create?q={encoded}&rt={rt}&FORM=GENCRE", data=payload
                )
                if "this prompt has been blocked" in response.text.lower():
                    logger.warning("Bing blocked the prompt.")
                    return None
                if response.status_code == 302:
                    break
            if response is not None and response.status_code != 302:
                body = response.text.lower()
                if any(marker in body for marker in BING_SIGNIN_MARKERS):
                    # Bing served the signed-out create page: the cookie is gone,
                    # not merely throttled. Skip straight to the alert threshold.
                    logger.error("Bing served a signed-out page — the _U cookie is invalid.")
                    self.consecutive_bing_failures = max(
                        self.consecutive_bing_failures, self.COOKIE_ALERT_AFTER - 1)

            if response is None or response.status_code != 302:
                # The usual cause is an expired cookie or a throttled IP.
                logger.warning(
                    f"Bing did not redirect (HTTP {getattr(response, 'status_code', '?')}) — "
                    "cookie likely expired or this IP is throttled."
                )
                return None

            redirect = response.headers.get("Location", "").replace("&nfy=1", "")
            if "id=" not in redirect:
                logger.warning("Bing redirect carried no request id.")
                return None

            request_id = redirect.split("id=")[-1]
            await client.get(f"{BING_URL}{redirect}")

            poll_url = f"{BING_URL}/images/create/async/results/{request_id}?q={encoded}"
            content = ""
            while True:
                poll = await client.get(poll_url)
                if poll.status_code != 200:
                    logger.warning(f"Bing polling returned HTTP {poll.status_code}.")
                    return None
                content = poll.text
                if content and "errorMessage" not in content:
                    break
                await asyncio.sleep(2)   # cancelled by wait_for when the budget ends

            links = {link.split("?w=")[0] for link in re.findall(r'src="([^"]+)"', content)}
            links -= BING_JUNK
            usable = [
                u for u in links
                if not u.endswith((".js", ".svg", ".gz.js"))
                and "clarity.ms" not in u
                and ("OIG" in u or "mm.bing.net" in u or "th/id/" in u)
            ]
            if not usable:
                logger.warning("Bing returned no usable image URLs.")
                return None

            img_response = await client.get(usable[0], headers={"User-Agent": "Mozilla/5.0"})
            if img_response.status_code != 200:
                return None
            return self._decode(img_response.content)

    # ── tier 3: the publisher's own photo ─────────────────────────────

    async def _from_url(self, url: str) -> Optional[Image.Image]:
        if not url or not url.startswith("http"):
            return None
        async with httpx.AsyncClient(timeout=self.STORY_IMAGE_TIMEOUT,
                                     follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                return None
            img = self._decode(r.content)
            # Tracking pixels and sprite sheets are common in RSS payloads.
            return img if img and min(img.size) >= 200 else None

    # ── tier 4: drawn locally, always available ───────────────────────

    def _branded_card(self, headline: str, category: str,
                      source_credit: str = "") -> Image.Image:
        """A composed headline card — the floor, not a bare gradient."""
        palette = COLOR_PALETTES.get(category, COLOR_PALETTES["default"])
        img = Image.new("RGB", (self.width, self.height), palette["start"])
        draw = ImageDraw.Draw(img)
        self._gradient(draw, palette["start"], palette["end"])

        accent = palette["accent"]
        margin = 88

        # Category eyebrow
        label = CATEGORY_LABELS.get(category, CATEGORY_LABELS["default"])
        eyebrow = self._font(26, bold=True)
        draw.text((margin, margin), " ".join(label), font=eyebrow, fill=accent)
        draw.line([(margin, margin + 52), (margin + 74, margin + 52)], fill=accent, width=4)

        # Headline, shrinking until it fits the available band
        max_width = self.width - margin * 2
        size, font, lines = 66, self._font(66, bold=True), []
        for size in (66, 60, 54, 48, 42, 38):
            font = self._font(size, bold=True)
            lines = self._wrap(headline, font, max_width)
            if len(lines) <= 5:
                break
        if len(lines) > 5:
            # Still too long at the smallest size — clip rather than overrun
            lines = lines[:5]
            lines[-1] = lines[-1].rstrip(" ,.;:") + "…"
        line_height = int(size * 1.28)
        block_height = line_height * len(lines)
        y = max(margin + 110, (self.height - block_height) // 2)

        for line in lines:
            # Offset shadow keeps the text legible over the lighter gradient end
            draw.text((margin + 2, y + 2), line, font=font, fill=(0, 0, 0))
            draw.text((margin, y), line, font=font, fill=(255, 255, 255))
            y += line_height

        # Footer rule, wordmark and attribution
        base = self.height - margin
        draw.line([(margin, base - 34), (self.width - margin, base - 34)],
                  fill=accent, width=2)
        foot = self._font(24, bold=True)
        draw.text((margin, base - 20), self.channel_name, font=foot, fill=(255, 255, 255))

        if source_credit:
            small = self._font(20)
            credit = f"Source: {source_credit}"[:70]
            width = small.getbbox(credit)[2] - small.getbbox(credit)[0]
            draw.text((self.width - margin - width, base - 18), credit,
                      font=small, fill=(215, 215, 225))
        return img

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _build_prompt(headline: str, category: str) -> str:
        style = STYLE_PREFIXES.get(category, DEFAULT_STYLE)
        return (
            f"{style}, dramatic lighting, shallow depth of field, 8K editorial photograph. "
            f"Visual concept: {headline[:160]}. "
            f"No text, no watermarks, no logos, no letters."
        )

    def _decode(self, raw: bytes) -> Optional[Image.Image]:
        """Bytes to a correctly framed RGB image, or None if it is not an image."""
        if not raw or len(raw) < 1024:
            return None
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
            return self._fit(img.convert("RGB"))
        except Exception as e:
            logger.warning(f"Could not decode image bytes: {type(e).__name__}: {e}")
            return None

    def _fit(self, img: Image.Image) -> Image.Image:
        """Cover-fit to 1280x720 without distorting the subject."""
        target = self.width / self.height
        w, h = img.size
        if w / h > target:
            new_w = int(h * target)
            img = img.crop(((w - new_w) // 2, 0, (w - new_w) // 2 + new_w, h))
        else:
            new_h = int(w / target)
            img = img.crop((0, (h - new_h) // 2, w, (h - new_h) // 2 + new_h))
        return img.resize((self.width, self.height), Image.Resampling.LANCZOS)

    def _gradient(self, draw: ImageDraw.ImageDraw, start: Tuple, end: Tuple):
        for y in range(self.height):
            r = y / self.height
            draw.line(
                [(0, y), (self.width, y)],
                fill=(
                    int(start[0] + (end[0] - start[0]) * r),
                    int(start[1] + (end[1] - start[1]) * r),
                    int(start[2] + (end[2] - start[2]) * r),
                ),
            )

    @staticmethod
    def _font(size: int, bold: bool = False):
        """
        Best available face at this size.

        The last two steps are what make the branded card unconditional: a
        container with no font files still renders, just at a fixed size.
        """
        for name in (FONT_CANDIDATES_BOLD if bold else FONT_CANDIDATES_REGULAR):
            try:
                return ImageFont.truetype(name, size)
            except (OSError, IOError):
                continue
        try:
            # Pillow >= 10.1 scales its bundled face to any size.
            return ImageFont.load_default(size=size)
        except Exception:
            return ImageFont.load_default()

    @staticmethod
    def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list:
        lines, current = [], ""
        for word in text.split():
            candidate = f"{current} {word}".strip()
            box = font.getbbox(candidate)
            if box[2] - box[0] <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines or ["Breaking News"]

    def _save(self, img: Image.Image) -> Optional[str]:
        """
        Writes the JPEG and confirms it is readable before handing back a path.

        The caller attaches this to a Telegram post, so a path that points at a
        truncated or zero-byte file is worse than no path at all.
        """
        name = f"post_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{random.randint(100, 999)}.jpg"
        path = os.path.join(self.output_dir, name)
        try:
            img.save(path, "JPEG", quality=self.JPEG_QUALITY, optimize=True, progressive=True)
            size = os.path.getsize(path)
            if size < 2048:
                logger.error(f"Saved image is implausibly small ({size} bytes): {path}")
                return None
            with Image.open(path) as check:
                check.verify()
            logger.info(f"Image saved: {path} ({size // 1024} KB)")
            return path
        except Exception as e:
            logger.error(f"Failed to save image: {type(e).__name__}: {e}", exc_info=True)
            return None
