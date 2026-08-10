"""
Image Generator.

Every published post must carry an image, so generation runs as a chain of
independent sources rather than one provider with a bare gradient behind it:

    1. Pollinations (Flux)  — ~6s, keyless, works from datacenter IPs
    2. Bing Image Creator   — ~90s, DALL-E 3 quality, needs a cookie that
                              expires and is throttled on datacenter IPs
    3. The publisher's own photo from the scraped story
    4. A branded headline card drawn locally

Tier 4 needs no network and no installed fonts, so `generate()` returning
None means the disk write itself failed — nothing else can produce it.

Everything here is async. The previous version was synchronous and was
called from inside the event loop, so a slow Bing request froze the whole
bot — scheduler, Telegram keepalives and the dashboard API alike — for up
to six minutes per post.
"""
import asyncio
import io
import logging
import os
import random
import re
import time
import urllib.parse
import zlib
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

POLLINATIONS_URL = "https://image.pollinations.ai/prompt/"

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

    # Per-tier ceilings. Bing is generous because its own polling loop can
    # legitimately take over a minute; the point is that it cannot run
    # unbounded and eat the posting slot.
    POLLINATIONS_TIMEOUT = 45.0
    BING_TIMEOUT = 80.0
    STORY_IMAGE_TIMEOUT = 20.0

    def __init__(self, output_dir: str, channel_name: str = "Novi News",
                 bing_cookie: str = "", total_budget: float = 150.0):
        self.output_dir = output_dir
        self.channel_name = channel_name
        self.bing_cookie = (bing_cookie or "").strip()
        self.total_budget = total_budget
        self.width = self.WIDTH
        self.height = self.HEIGHT

        # Counters the dashboard reports, so a silent slide onto the
        # last-resort card is visible instead of being discovered in the feed.
        self.stats = {"pollinations": 0, "bing": 0, "story_image": 0, "card": 0, "failed": 0}
        self.last_source = ""

        os.makedirs(output_dir, exist_ok=True)
        logger.info(f"Image Generator ready. Output: {output_dir} | "
                    f"Bing cookie: {'present' if self.bing_cookie else 'absent'}")

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

        for name, budget, coro_factory in (
            ("pollinations", self.POLLINATIONS_TIMEOUT, lambda: self._from_pollinations(prompt)),
            ("bing", self.BING_TIMEOUT, lambda: self._from_bing(prompt)),
            ("story_image", self.STORY_IMAGE_TIMEOUT, lambda: self._from_url(story_image_url)),
        ):
            if name == "bing" and not self.bing_cookie:
                continue
            if name == "story_image" and not story_image_url:
                continue

            remaining = left()
            if remaining < 5:
                logger.warning(f"Image budget exhausted before '{name}'; using branded card.")
                break

            try:
                img = await asyncio.wait_for(coro_factory(), timeout=min(budget, remaining))
            except asyncio.TimeoutError:
                logger.warning(f"Image source '{name}' timed out.")
                img = None
            except Exception as e:
                logger.warning(f"Image source '{name}' failed: {type(e).__name__}: {e}")
                img = None

            if img is not None:
                tier = name
                logger.info(f"Image sourced from '{name}'.")
                break

        if img is None:
            logger.warning("All image sources unavailable — drawing branded headline card.")
            img = self._branded_card(headline, category, source_credit)

        path = self._save(img)
        if path:
            self.stats[tier] = self.stats.get(tier, 0) + 1
            self.last_source = tier
        else:
            self.stats["failed"] += 1
            self.last_source = "failed"
        return path

    # ── tier 1: Pollinations ──────────────────────────────────────────

    def _pollinations_url(self, prompt: str, seed: int) -> str:
        return (
            POLLINATIONS_URL
            + urllib.parse.quote(prompt[:900], safe="")
            + f"?width={self.width}&height={self.height}"
            f"&model=flux&nologo=true&seed={seed}"
        )

    def hosted_prompt_url(self, headline: str, category: str = "default") -> str:
        """
        A public image URL for this headline that needs no hosting of our own.

        Used as the website's hero-image fallback when Supabase Storage is not
        set up yet, so an article still gets a picture and a social preview.
        The seed is derived from the headline, so the URL is stable and keeps
        resolving to the same picture on every request.
        """
        seed = zlib.crc32(headline.encode("utf-8", "ignore")) % 10_000_000
        return self._pollinations_url(self._build_prompt(headline, category), seed)

    async def _from_pollinations(self, prompt: str) -> Optional[Image.Image]:
        """Keyless Flux endpoint. Fast and unbothered by datacenter IPs."""
        url = self._pollinations_url(prompt, random.randint(1, 10_000_000))
        async with httpx.AsyncClient(timeout=self.POLLINATIONS_TIMEOUT,
                                     follow_redirects=True) as client:
            r = await client.get(url)
            if r.status_code != 200:
                logger.warning(f"Pollinations returned HTTP {r.status_code}.")
                return None
            if not r.headers.get("content-type", "").startswith("image/"):
                logger.warning("Pollinations returned a non-image response.")
                return None
            return self._decode(r.content)

    # ── tier 2: Bing Image Creator ────────────────────────────────────

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
