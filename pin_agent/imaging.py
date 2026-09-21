"""
Pin image builder.

Pinterest ranks 2:3 vertical images (1000x1500). AliExpress product photos are
square, frequently watermarked, and often collaged with the seller's own text,
so they cannot be posted as-is: they would be letterboxed, and republishing a
supplier's composite wholesale is a poorer look than a consistent template.

The product photo is placed into a branded frame instead. That fixes the
aspect ratio, gives every pin the same recognisable style, and puts our own
wordmark on the image rather than another shop's.

Everything is drawn with Pillow, and the last fallback needs no installed
fonts, so a bare container still produces a pin.
"""
import asyncio
import io
import logging
import os
import re
from datetime import datetime
from typing import List, Optional, Tuple

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger("PinAgent.Imaging")

PIN_WIDTH = 1000
PIN_HEIGHT = 1500

# Warm, kitchen-appropriate palette. Deliberately not the news site's blue:
# this is a shopping brand and should not look like the newspaper.
PALETTE = {
    "paper": (250, 247, 242),
    "ink": (28, 30, 34),
    "muted": (110, 114, 120),
    "accent": (198, 106, 58),      # terracotta
    "band": (255, 255, 255),
}

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


class PinImageBuilder:
    """Composites a product photo into a branded 1000x1500 pin."""

    DOWNLOAD_TIMEOUT = 25.0

    def __init__(self, output_dir: str, brand: str = "Novi"):
        self.output_dir = output_dir
        self.brand = brand
        os.makedirs(output_dir, exist_ok=True)

    # ── fonts ────────────────────────────────────────────────────

    @staticmethod
    def _font(size: int, bold: bool = False):
        """
        Best available font at this size.

        Falls back to Pillow's bundled face, which is always present, so the
        pin still renders on a container with no fonts installed.
        """
        for name in (FONT_CANDIDATES_BOLD if bold else FONT_CANDIDATES_REGULAR):
            try:
                return ImageFont.truetype(name, size)
            except Exception:
                continue
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    # ── product photo ────────────────────────────────────────────

    async def fetch_photo(self, url: str) -> Optional[Image.Image]:
        """
        The photo for a pin, from the web or from disk.

        A GENERATED picture arrives as a file rather than a URL -- it was
        never on the internet -- so this accepts a path too. Everything
        after the load is identical, including the too-small check.
        """
        if not url:
            return None

        if not url.startswith("http"):
            if not os.path.exists(url):
                return None
            try:
                image = Image.open(url)
                image.load()
                if image.width < 300 or image.height < 300:
                    logger.warning(f"Generated picture too small: "
                                   f"{image.size}.")
                    return None
                return image.convert("RGB")
            except Exception as e:
                logger.warning(f"Generated picture unusable: "
                               f"{type(e).__name__}: {e}")
                return None
        try:
            async with httpx.AsyncClient(timeout=self.DOWNLOAD_TIMEOUT,
                                         follow_redirects=True) as client:
                response = await client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0"})
            if response.status_code != 200:
                logger.warning(f"Product photo returned HTTP {response.status_code}.")
                return None
            image = Image.open(io.BytesIO(response.content))
            image.load()
            if image.width < 300 or image.height < 300:
                logger.warning(f"Product photo too small: {image.size}.")
                return None
            return image.convert("RGB")
        except Exception as e:
            logger.warning(f"Product photo unusable: {type(e).__name__}: {e}")
            return None

    # ── layout helpers ───────────────────────────────────────────

    @staticmethod
    def _cover(image: Image.Image, box: Tuple[int, int]) -> Image.Image:
        """Scales and centre-crops to fill the box without distorting."""
        target_w, target_h = box
        scale = max(target_w / image.width, target_h / image.height)
        resized = image.resize((max(1, round(image.width * scale)),
                                max(1, round(image.height * scale))),
                               Image.LANCZOS)
        left = (resized.width - target_w) // 2
        top = (resized.height - target_h) // 2
        return resized.crop((left, top, left + target_w, top + target_h))

    @staticmethod
    def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int,
              max_lines: int) -> List[str]:
        """Greedy word wrap, ellipsised if it will not fit."""
        words = (text or "").split()
        lines: List[str] = []
        current = ""

        for word in words:
            candidate = f"{current} {word}".strip()
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
            current = word
            if len(lines) == max_lines:
                break

        if current and len(lines) < max_lines:
            lines.append(current)

        if lines and len(lines) == max_lines:
            last = lines[-1]
            while last and draw.textlength(last + "…", font=font) > max_width:
                last = last[:-1].rstrip()
            if len(" ".join(lines)) < len(text or ""):
                lines[-1] = last + "…"
        return lines

    # ── the pin ──────────────────────────────────────────────────

    def compose(self, photo: Optional[Image.Image], title: str,
                eyebrow: str = "") -> Image.Image:
        """
        Builds the pin.

        Layout is photo on top, text on a light band beneath. A caption band
        rather than text over the photo, because product photos are busy and
        overlaid text on them is unreadable at feed size.
        """
        canvas = Image.new("RGB", (PIN_WIDTH, PIN_HEIGHT), PALETTE["paper"])
        draw = ImageDraw.Draw(canvas)

        # The photo carries the pin in a visual feed, so it takes most of the
        # height; the band below is sized to the copy rather than left as a
        # fixed slab with dead space under the title.
        photo_h = 1080
        if photo is not None:
            canvas.paste(self._cover(photo, (PIN_WIDTH, photo_h)), (0, 0))
        else:
            # No photo: a soft wash so the pin is still a finished object.
            wash = Image.new("RGB", (PIN_WIDTH, photo_h), PALETTE["accent"])
            canvas.paste(wash.filter(ImageFilter.GaussianBlur(2)), (0, 0))

        # Thin accent rule separating image from copy
        draw.rectangle([(0, photo_h), (PIN_WIDTH, photo_h + 8)], fill=PALETTE["accent"])

        margin = 70
        y = photo_h + 46

        # THE WORDMARK SITS AT THE TOP OF THE BAND, not the bottom.
        #
        # Pinterest draws its own controls under the creative -- the Direct
        # Links call-to-action button lives there -- and its guidance is to
        # keep text and logos a clear margin from the edges so nothing is
        # trimmed or crowded. The brand line used to sit 46px from the
        # bottom, inside that strip.
        #
        # It could not simply move up: a three-line title already reaches
        # within a hundred pixels of where it was. So it moved to the far
        # end of the eyebrow row instead, which was empty, and the whole
        # bottom of the pin is now clear.
        font_brand = self._font(30, bold=True)
        brand_w = draw.textlength(self.brand, font=font_brand)
        brand_x = PIN_WIDTH - margin - brand_w
        draw.ellipse([(brand_x - 26, y + 9), (brand_x - 10, y + 25)],
                     fill=PALETTE["accent"])
        draw.text((brand_x, y), self.brand, font=font_brand,
                  fill=PALETTE["muted"])

        if eyebrow:
            font_eyebrow = self._font(30, bold=True)
            # Trimmed so a long board name cannot run into the wordmark.
            room = PIN_WIDTH - margin * 2 - brand_w - 60
            label = eyebrow.upper()[:34]
            while label and draw.textlength(label, font=font_eyebrow) > room:
                label = label[:-1]
            draw.text((margin, y), label.rstrip(),
                      font=font_eyebrow, fill=PALETTE["accent"])
        y += 56

        font_title = self._font(56, bold=True)
        lines = self._wrap(draw, title, font_title, PIN_WIDTH - margin * 2, 3)
        for line in lines:
            draw.text((margin, y), line, font=font_title, fill=PALETTE["ink"])
            y += 68

        return canvas

    async def build(self, product: dict, title: str, eyebrow: str = "",
                    require_photo: bool = False) -> Tuple[Optional[str], bytes]:
        """
        Produces the finished pin file.

        Returns (path, png_bytes). The bytes are returned too so the caller can
        fingerprint the image for deduplication without reading it back.

        `require_photo` refuses to build rather than falling back to the
        accent wash. A product pin without its photograph is still a pin --
        the copy carries it. An ADVICE pin is nothing but a photograph and a
        sentence, so a coloured rectangle with text on it would be the most
        obviously automated thing on the board, and the tip bank has other
        tips to try instead.
        """
        photo = None
        for url in (product.get("images") or [])[:3]:
            photo = await self.fetch_photo(url)
            if photo is not None:
                break

        if photo is None:
            logger.warning(f"No usable photo for '{product.get('title', '')[:40]}'.")
            if require_photo:
                return None, b""

        canvas = await asyncio.to_thread(self.compose, photo, title, eyebrow)

        buffer = io.BytesIO()
        canvas.save(buffer, format="JPEG", quality=88, optimize=True)
        data = buffer.getvalue()

        slug = re.sub(r"[^a-z0-9]+", "-", (title or "pin").lower()).strip("-")[:40]
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.output_dir, f"pin_{stamp}_{slug or 'pin'}.jpg")

        try:
            with open(path, "wb") as fh:
                fh.write(data)
        except OSError as e:
            logger.error(f"Could not write the pin image: {e}")
            return None, data

        logger.info(f"Pin image built: {path} ({len(data) // 1024} KB)")
        return path, data

    @property
    def has_photo_source(self) -> bool:
        return True
