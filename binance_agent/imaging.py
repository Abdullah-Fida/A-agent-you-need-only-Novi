"""
Builds the picture that goes with a Binance Square post.

A REAL PHOTOGRAPH, never a generated one -- same rule as the website. The
photo comes from Openverse under a licence that permits commercial use and
cropping, and if none is found the post goes out without a picture rather
than with an invented one.

Under the photograph sits a band carrying the numbers: the ticker, the move,
the price and the volume. That band is the reason the image is worth having
at all. A stock photo of physical bitcoin coins says nothing; the same photo
with "$ARB +12.7% · $67M volume" underneath is a reason to stop scrolling,
and Square shows images at a size where that text is readable.

1200x675 (16:9), which is what Square renders without cropping.
"""
import logging
import os
from datetime import datetime
from typing import Dict, Optional, Tuple

logger = logging.getLogger("BinanceAgent.Imaging")

WIDTH, HEIGHT = 1200, 675
BAND = 190                      # the data band along the bottom

# Deliberately not the news site's palette. This is a market note, not a
# newspaper, and it should not look like one.
INK = (16, 18, 22)
PAPER = (248, 249, 251)
MUTED = (128, 134, 145)
UP = (14, 168, 106)
DOWN = (222, 62, 68)
BINANCE_GOLD = (240, 185, 11)

FONTS_BOLD = ["C:\\Windows\\Fonts\\arialbd.ttf", "arialbd.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "DejaVuSans-Bold.ttf"]
FONTS_REG = ["C:\\Windows\\Fonts\\arial.ttf", "arial.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
             "DejaVuSans.ttf"]

# WHY THESE ARE NEUTRAL AND NOT PER-COIN.
#
# The obvious design maps each ticker to a picture of that coin. It does not
# survive contact with reality: searching a coin name returns a picture of
# BITCOIN almost every time, because that is what stock libraries have. The
# first draft built put a gold Bitcoin on a post about UNI, which is not a
# weak illustration -- it is a misleading one, and on a market post that
# matters more than it would on a news article.
#
# So the subjects are deliberately generic to trading and infrastructure.
# None can be read as claiming to be a particular asset. Only Bitcoin gets
# Bitcoin imagery, because there it is simply true.
#
# Every query here is verified against Openverse AND looked at on the
# Wikimedia fallback, so a draft still gets a sensible picture when Openverse
# is down -- which is exactly what happened on 2 September 2026, when the
# first live draft went out bare.
#
# The list is SHORT on purpose. Eleven other subjects from the same verified
# vocabulary were tried and dropped after looking at what they actually
# return: "office workspace" gave a lipstick flatlay, "data chart" an
# aircraft negative, "laptop screen" a confused elderly woman, "microchip" an
# unreadable brown blur, "financial report documents" a 1939 Polish ledger,
# and "inflation money currency" a line graph, which on a price post reads as
# a claim about this coin. Five subjects that are always right beat eight
# where three are wrong; the photograph still varies inside each subject,
# because `exclude` carries the last dozen pictures used.
#
# Bitcoin gets "bitcoin coin", not "bitcoin cryptocurrency": the latter
# returns a Bitcoin resting on a judge's GAVEL, which reads as a regulation
# story rather than a price move.
BTC_QUERY = "bitcoin coin"
NEUTRAL_QUERIES = [
    "stock market",        # a macro shot of a chart on screen
    "bank building",
    "credit cards",
    "dollar bills",
    "smartphone",
]
DEFAULT_QUERY = "stock market"


def _font(paths, size):
    from PIL import ImageFont
    for p in paths:
        try:
            return ImageFont.truetype(p, int(size))
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _money(v: float) -> str:
    if v >= 1000:
        return f"${v:,.0f}"
    if v >= 1:
        return f"${v:,.2f}"
    if v >= 0.01:
        return f"${v:.4f}"
    return f"${v:.6f}".rstrip("0")


def _big(v: float) -> str:
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    if v >= 1e6:
        return f"${v/1e6:.0f}M"
    return f"${v:,.0f}"


class DraftImage:
    """Photograph plus a data band. Returns a local path, or "" if no
    usable photograph could be found."""

    def __init__(self, photos=None, output_dir: str = "assets/binance",
                 brand: str = "PressVane"):
        self.photos = photos
        self.output_dir = output_dir
        self.brand = brand
        self.last_error = ""
        self.last_credit = ""
        self._last_url = ""
        os.makedirs(output_dir, exist_ok=True)

    @staticmethod
    def query_for(base: str) -> str:
        """
        Bitcoin gets Bitcoin. Everything else gets a neutral trading subject,
        chosen from the ticker so it is stable for a coin but varied across
        the feed -- three posts in a row on the same trading floor would be
        as obvious as three posts with no picture at all.
        """
        base = (base or "").upper()
        if base == "BTC":
            return BTC_QUERY
        if not base:
            return DEFAULT_QUERY
        return NEUTRAL_QUERIES[sum(base.encode()) % len(NEUTRAL_QUERIES)]

    async def build(self, coin: Dict, exclude=()) -> Tuple[str, str]:
        """
        Returns (local_path, credit). Both empty when nothing usable exists.

        `exclude` carries photo URLs already used, so two drafts in a row do
        not carry the same picture -- the same mistake the website made.
        """
        if not self.photos:
            self.last_error = "no photo finder wired"
            return "", ""

        try:
            url, credit = await self.photos.find(self.query_for(coin["base"]),
                                                 exclude=exclude)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.warning(f"Photo lookup failed: {self.last_error}")
            return "", ""
        if not url:
            self.last_error = "no openly-licensed photograph found"
            return "", ""
        # Recorded so the caller can exclude it next time and two drafts in
        # a row never carry the same picture.
        self._last_url = url

        blob = await self._download(url)
        if not blob:
            return "", ""

        try:
            path = self._compose(blob, coin)
        except Exception as e:
            self.last_error = f"compose failed: {type(e).__name__}: {e}"
            logger.error(self.last_error)
            return "", ""

        self.last_credit = credit
        return path, credit

    async def _download(self, url: str) -> Optional[bytes]:
        import httpx
        # Wikimedia and others answer 429 to a generic user agent. Say who
        # we are and give a contact, which is what their policy asks for.
        headers = {"User-Agent": ("PressVane/1.0 (+https://pressvane.com; "
                                  "market note illustration) python-httpx")}
        try:
            async with httpx.AsyncClient(timeout=40, follow_redirects=True,
                                         headers=headers) as c:
                r = await c.get(url)
            if r.status_code != 200:
                self.last_error = f"photo download HTTP {r.status_code}"
                logger.warning(self.last_error)
                return None
            if len(r.content) > 12 * 1024 * 1024:
                self.last_error = "photo over 12MB, skipped"
                return None
            return r.content
        except Exception as e:
            self.last_error = f"photo download failed: {type(e).__name__}"
            logger.warning(self.last_error)
            return None

    def _compose(self, blob: bytes, coin: Dict) -> str:
        import io as _io
        from PIL import Image, ImageDraw

        photo = Image.open(_io.BytesIO(blob)).convert("RGB")
        photo_h = HEIGHT - BAND

        # Cover-crop: fill the space and centre, never squash. A stretched
        # photograph is the loudest possible sign nobody looked at it.
        scale = max(WIDTH / photo.width, photo_h / photo.height)
        photo = photo.resize((max(1, int(photo.width * scale)),
                              max(1, int(photo.height * scale))),
                             Image.LANCZOS)
        left = (photo.width - WIDTH) // 2
        top = (photo.height - photo_h) // 2
        photo = photo.crop((left, top, left + WIDTH, top + photo_h))

        canvas = Image.new("RGB", (WIDTH, HEIGHT), PAPER)
        canvas.paste(photo, (0, 0))
        d = ImageDraw.Draw(canvas)

        up = coin["change"] >= 0
        accent = UP if up else DOWN
        d.rectangle([(0, photo_h), (WIDTH, photo_h + 6)], fill=accent)

        y = photo_h + 6 + 26
        pad = 44

        f_tick = _font(FONTS_BOLD, 62)
        f_move = _font(FONTS_BOLD, 62)
        f_lbl = _font(FONTS_REG, 22)
        f_val = _font(FONTS_BOLD, 30)
        f_brand = _font(FONTS_REG, 22)

        ticker = f"${coin['base']}"
        d.text((pad, y), ticker, font=f_tick, fill=INK)
        tw = d.textlength(ticker, font=f_tick)

        move = f"{'+' if up else ''}{coin['change']:.1f}%"
        d.text((pad + tw + 26, y), move, font=f_move, fill=accent)

        # Price and volume, right-aligned so the block reads as a data strip
        # rather than a caption.
        cols = [("PRICE", _money(coin["price"])),
                ("24H VOLUME", _big(coin["volume"])),
                ("TRADES", f"{coin['trades']:,}")]
        x = WIDTH - pad
        for label, value in reversed(cols):
            wv = d.textlength(value, font=f_val)
            wl = d.textlength(label, font=f_lbl)
            w = max(wv, wl)
            d.text((x - w, y + 2), label, font=f_lbl, fill=MUTED)
            d.text((x - w, y + 34), value, font=f_val, fill=INK)
            x -= w + 52

        d.ellipse([(pad, HEIGHT - 40), (pad + 11, HEIGHT - 29)],
                  fill=BINANCE_GOLD)
        d.text((pad + 22, HEIGHT - 44), self.brand, font=f_brand, fill=MUTED)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.output_dir,
                            f"bnb_{coin['base'].lower()}_{stamp}.jpg")
        canvas.save(path, "JPEG", quality=90)
        logger.info(f"Draft image built: {path}")
        return path
