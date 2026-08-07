"""
Image Generator Module.
Uses Bing DALL-E 3 for photorealistic, 
cinematic AI-generated news thumbnails.

Direct httpx implementation to avoid BingImageCreator's pkg_resources dependency
which breaks on Render/Linux (Python 3.12+).
"""
import logging
import os
import io
import re
import random
import asyncio
import threading
from datetime import datetime
from typing import Optional
import urllib.request
import urllib.parse

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

import time

logger = logging.getLogger("OmniBot.ImageGen")

# ── Bing Image Creator constants ─────────────────────────
BING_URL = "https://www.bing.com"
FORWARDED_IP = f"13.{random.randint(104, 107)}.{random.randint(0, 255)}.{random.randint(0, 255)}"
BING_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "max-age=0",
    "content-type": "application/x-www-form-urlencoded",
    "referrer": "https://www.bing.com/images/create/",
    "origin": "https://www.bing.com",
    "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36 Edg/110.0.1587.63",
    "x-forwarded-for": FORWARDED_IP,
}

# ── Color palettes for different content categories ───────────────────
COLOR_PALETTES = {
    "tech_ai": {
        "gradient_start": (15, 23, 42),
        "gradient_end": (59, 130, 246),
        "accent": (139, 92, 246),
        "text": (255, 255, 255),
    },
    "tech": {
        "gradient_start": (15, 23, 42),
        "gradient_end": (59, 130, 246),
        "accent": (139, 92, 246),
        "text": (255, 255, 255),
    },
    "business_markets": {
        "gradient_start": (6, 78, 59),
        "gradient_end": (16, 185, 129),
        "accent": (245, 158, 11),
        "text": (255, 255, 255),
    },
    "business": {
        "gradient_start": (6, 78, 59),
        "gradient_end": (16, 185, 129),
        "accent": (245, 158, 11),
        "text": (255, 255, 255),
    },
    "world_news": {
        "gradient_start": (127, 29, 29),
        "gradient_end": (239, 68, 68),
        "accent": (251, 191, 36),
        "text": (255, 255, 255),
    },
    "politics": {
        "gradient_start": (30, 20, 60),
        "gradient_end": (100, 40, 120),
        "accent": (220, 180, 60),
        "text": (255, 255, 255),
    },
    "sports": {
        "gradient_start": (10, 50, 30),
        "gradient_end": (20, 140, 70),
        "accent": (255, 200, 50),
        "text": (255, 255, 255),
    },
    "crypto": {
        "gradient_start": (20, 10, 40),
        "gradient_end": (120, 60, 200),
        "accent": (255, 180, 50),
        "text": (255, 255, 255),
    },
    "pakistan": {
        "gradient_start": (0, 60, 30),
        "gradient_end": (0, 130, 60),
        "accent": (255, 255, 255),
        "text": (255, 255, 255),
    },
    "default": {
        "gradient_start": (30, 30, 50),
        "gradient_end": (80, 80, 120),
        "accent": (100, 200, 255),
        "text": (255, 255, 255),
    }
}

CATEGORY_LABELS = {
    "tech_ai": "TECH / AI",
    "tech": "TECH",
    "business_markets": "BUSINESS",
    "business": "BUSINESS",
    "world_news": "WORLD",
    "politics": "POLITICS",
    "sports": "SPORTS",
    "crypto": "CRYPTO",
    "pakistan": "PAKISTAN",
}


class ImageGenerator:
    """
    Generates branded post header images using Bing Image Creator (DALL-E 3)
    for stunning AI backgrounds with professional Pillow text overlays.
    
    Uses direct httpx calls to Bing Image Creator API instead of the 
    BingImageCreator library to avoid pkg_resources/regex dependency issues
    on Render (Linux/Python 3.12+).
    """
    
    def __init__(self, output_dir: str, channel_name: str = "Novi News",
                 bing_cookie: str = ""):
        self.output_dir = output_dir
        self.channel_name = channel_name
        self.bing_cookie = bing_cookie
        self.width = 1280
        self.height = 720
        
        os.makedirs(output_dir, exist_ok=True)
        logger.info(f"Image Generator initialized. Output: {output_dir}")
    
    async def _fetch_bing_image_async(self, prompt: str) -> Optional[Image.Image]:
        """
        Calls Bing Image Creator (DALL-E 3) to generate an image using direct httpx.
        This avoids the BingImageCreator library which depends on pkg_resources/regex 
        that break on Render's Python 3.12+ environment.
        
        Returns a PIL Image or None on failure.
        """
        if not self.bing_cookie:
            logger.error("No Bing cookie provided.")
            return None
        
        try:
            url_encoded_prompt = urllib.parse.quote(prompt)
            
            async with httpx.AsyncClient(
                headers=BING_HEADERS, 
                cookies={"_U": self.bing_cookie},
                timeout=httpx.Timeout(200.0),
                follow_redirects=False,
            ) as client:
                # Step 1: Submit the image generation request (try rt=3 first, then rt=4)
                url = f"{BING_URL}/images/create?q={url_encoded_prompt}&rt=3&FORM=GENCRE"
                payload = f"q={url_encoded_prompt}&qs=ds"
                
                response = await client.post(url, data=payload)
                
                if "this prompt has been blocked" in response.text.lower():
                    logger.warning("Bing blocked the prompt. Try different wording.")
                    return None
                
                if response.status_code != 302:
                    # Try rt=4
                    url = f"{BING_URL}/images/create?q={url_encoded_prompt}&rt=4&FORM=GENCRE"
                    response = await client.post(url, data=payload)
                    if response.status_code != 302:
                        logger.warning(f"Bing did not redirect. Status: {response.status_code}")
                        return None
                
                # Step 2: Follow redirect to get the request ID
                redirect_url = response.headers.get("Location", "").replace("&nfy=1", "")
                if not redirect_url or "id=" not in redirect_url:
                    logger.warning(f"No redirect URL from Bing. Headers: {dict(response.headers)}")
                    return None
                    
                request_id = redirect_url.split("id=")[-1]
                await client.get(f"{BING_URL}{redirect_url}")
                
                # Step 3: Poll for results
                polling_url = f"{BING_URL}/images/create/async/results/{request_id}?q={url_encoded_prompt}"
                
                start_wait = time.time()
                while True:
                    if time.time() - start_wait > 120:  # 2 minute timeout
                        logger.warning("Bing image generation timed out after 120s.")
                        return None
                    
                    poll_response = await client.get(polling_url)
                    if poll_response.status_code != 200:
                        logger.warning(f"Bing polling returned status {poll_response.status_code}")
                        return None
                    
                    content = poll_response.text
                    if content and "errorMessage" not in content:
                        break
                    
                    await asyncio.sleep(2)
                
                # Step 4: Extract image URLs using stdlib re (not regex)
                image_links = re.findall(r'src="([^"]+)"', content)
                # Remove size limit parameters
                image_links = [link.split("?w=")[0] for link in image_links]
                # Remove duplicates
                image_links = list(set(image_links))
                
                # Filter out bad/non-image URLs
                bad_images = [
                    "https://r.bing.com/rp/in-2zU3AJUdkgFe7ZKv19yPBHVs.png",
                    "https://r.bing.com/rp/TX9QuO3WzcCJz1uaaSwQAz39Kb0.jpg",
                ]
                valid_links = [
                    url for url in image_links 
                    if url not in bad_images 
                    and not url.endswith('.js') 
                    and not url.endswith('.svg')
                    and not url.endswith('.gz.js')
                    and 'clarity.ms' not in url
                    and ('OIG' in url or 'mm.bing.net' in url or 'th/id/' in url)
                ]
                
                if not valid_links:
                    # Fallback: try any image-looking URL
                    valid_links = [
                        url for url in image_links 
                        if url not in bad_images 
                        and not url.endswith('.js')
                        and not url.endswith('.svg')
                        and not url.endswith('.gz.js')
                        and 'clarity.ms' not in url
                    ]
                
                logger.info(f"Bing Image URLs found: {len(image_links)} total, {len(valid_links)} valid")
                
                if not valid_links:
                    logger.warning("No valid image URLs found from Bing.")
                    return None
                
                # Step 5: Download the best image
                img_url = valid_links[0]
                logger.info(f"Downloading Bing image: {img_url}")
                
                img_response = await client.get(img_url, headers={"User-Agent": "Mozilla/5.0"})
                if img_response.status_code != 200:
                    logger.warning(f"Failed to download Bing image. Status: {img_response.status_code}")
                    return None
                
                img = Image.open(io.BytesIO(img_response.content)).convert("RGB")
                
                # Bing images are 1024x1024. Resize and crop to 1280x720
                img = img.resize((self.width, self.width), Image.Resampling.LANCZOS)
                top = (self.width - self.height) // 2
                bottom = top + self.height
                img = img.crop((0, top, self.width, bottom))
                return img
                
        except Exception as e:
            logger.warning(f"Bing Image generation failed: {e}", exc_info=True)
            return None
    
    def _fetch_bing_image(self, prompt: str) -> Optional[Image.Image]:
        """
        Synchronous wrapper around the async Bing image fetch.
        Handles being called from both sync and async contexts safely.
        """
        async def _run():
            return await self._fetch_bing_image_async(prompt)
        
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        if loop.is_running():
            # We're inside an async context (e.g., called from content_engine).
            # Run in a separate thread with its own event loop.
            result = None
            error = None
            
            def run_in_thread():
                nonlocal result, error
                try:
                    result = asyncio.run(_run())
                except Exception as e:
                    error = e
            
            t = threading.Thread(target=run_in_thread)
            t.start()
            t.join(timeout=180)  # 3-minute max wait
            
            if error:
                logger.warning(f"Bing image thread failed: {error}", exc_info=True)
                return None
            if t.is_alive():
                logger.warning("Bing image thread timed out after 180s.")
                return None
                
            return result
        else:
            return loop.run_until_complete(_run())

    def _create_gradient(self, draw: ImageDraw.Draw, 
                         color_start: tuple, color_end: tuple):
        """Draws a smooth vertical gradient on the image."""
        for y in range(self.height):
            ratio = y / self.height
            r = int(color_start[0] + (color_end[0] - color_start[0]) * ratio)
            g = int(color_start[1] + (color_end[1] - color_start[1]) * ratio)
            b = int(color_start[2] + (color_end[2] - color_start[2]) * ratio)
            draw.line([(0, y), (self.width, y)], fill=(r, g, b))
    
    def _get_font(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        """Gets a font, falling back to default if custom font not found."""
        font_names = []
        if bold:
            font_names = ["arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf", 
                          "segoeui.ttf", "calibrib.ttf"]
        else:
            font_names = ["arial.ttf", "Arial.ttf", "DejaVuSans.ttf",
                          "segoeui.ttf", "calibri.ttf"]
        
        for font_name in font_names:
            try:
                return ImageFont.truetype(font_name, size)
            except (OSError, IOError):
                continue
        
        # Absolute fallback
        try:
            return ImageFont.truetype("C:\\Windows\\Fonts\\arial.ttf", size)
        except:
            return ImageFont.load_default()
    
    def _wrap_text(self, text: str, font: ImageFont.FreeTypeFont, 
                   max_width: int) -> list:
        """Wraps text to fit within a maximum pixel width."""
        words = text.split()
        lines = []
        current_line = ""
        
        for word in words:
            test_line = f"{current_line} {word}".strip()
            bbox = font.getbbox(test_line)
            text_width = bbox[2] - bbox[0]
            
            if text_width <= max_width:
                current_line = test_line
            else:
                if current_line:
                    lines.append(current_line)
                current_line = word
        
        if current_line:
            lines.append(current_line)
        
        return lines
    
    def _add_decorative_elements(self, draw: ImageDraw.Draw, accent_color: tuple):
        """Adds subtle geometric decorations to make the image feel premium."""
        # Top-left corner accent line
        draw.line([(40, 40), (200, 40)], fill=accent_color, width=4)
        draw.line([(40, 40), (40, 100)], fill=accent_color, width=4)
        
        # Bottom-right corner accent
        draw.line([(self.width - 200, self.height - 40), 
                   (self.width - 40, self.height - 40)], fill=accent_color, width=4)
        draw.line([(self.width - 40, self.height - 100), 
                   (self.width - 40, self.height - 40)], fill=accent_color, width=4)
        
        # Subtle dot pattern (decorative)
        for i in range(5):
            x = random.randint(50, self.width - 50)
            y = random.randint(50, self.height - 50)
            size = random.randint(2, 5)
            draw.ellipse([(x, y), (x + size, y + size)], fill=accent_color)
    
    def generate(self, headline: str, category: str = "default",
                 source_credit: str = "") -> Optional[str]:
        """
        Generates a branded news image with headline overlay.
        
        Pipeline:
          1. Try Bing DALL-E 3 for a cinematic AI background
          2. Fall back to Pillow gradient if Bing is unavailable
          3. Overlay brand elements: category badge, headline, watermark, timestamp
        
        Args:
            headline: The main headline text (5-15 words)
            category: Content category for color theming
            source_credit: Source attribution text
        
        Returns:
            Path to the saved image, or None on failure.
        """
        try:
            palette = COLOR_PALETTES.get(category, COLOR_PALETTES["default"])
            
            # Category-specific prompt templates for better variety
            if "tech" in category:
                style_prefix = "Cyberpunk, neon lights, high-tech server room, glowing futuristic aesthetic"
            elif "business" in category:
                style_prefix = "Modern glass skyscraper office, Wall Street, professional stock market chart aesthetic"
            elif category == "world_news":
                style_prefix = "Global map hologram, UN assembly hall, professional international news broadcasting desk"
            elif category == "crypto":
                style_prefix = "Golden Bitcoin coins, blockchain network visualization, futuristic digital currency hologram, dark moody lighting"
            elif category == "pakistan":
                style_prefix = "Pakistani cityscape, modern Islamabad or Karachi skyline, South Asian photojournalism"
            elif category == "politics":
                style_prefix = "Parliament building, debate hall, serious political photojournalism, majestic"
            elif category == "sports":
                style_prefix = "Massive sports stadium, dramatic floodlights, intense cinematic action photography"
            else:
                style_prefix = "Ultra high quality cinematic photojournalism photograph"
            
            ai_prompt = (
                f"{style_prefix}, dramatic lighting, shallow depth of field, 8K resolution, "
                f"news category: {category}. "
                f"Visual concept: {headline}. "
                f"No text, no watermarks, no logos, pure photography."
            )
            
            # ── Step 1: Fetch AI Background ───────────────────────
            max_retries = 2
            retry_delay = 5
            img = None
            
            for attempt in range(max_retries):
                img = self._fetch_bing_image(ai_prompt)
                if img:
                    break
                else:
                    if attempt < max_retries - 1:
                        logger.warning(f"Bing Image fetch failed (attempt {attempt + 1}). Retrying in {retry_delay}s...")
                        time.sleep(retry_delay)
            
            # Fallback to gradient if AI image failed
            if not img:
                logger.warning("Bing AI image failed, using gradient fallback.")
                img = Image.new("RGB", (self.width, self.height))
                draw = ImageDraw.Draw(img)
                self._create_gradient(draw, palette["gradient_start"], palette["gradient_end"])
                self._add_decorative_elements(draw, palette["accent"])
            else:
                logger.info("Successfully fetched pure AI background from Bing DALL-E 3.")
            
            # ── Step 2: Save Pure Image ──────────────────────────────────────
            filename = f"post_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{random.randint(100,999)}.png"
            filepath = os.path.join(self.output_dir, filename)
            img.save(filepath, "PNG", quality=95)
            
            logger.info(f"Generated pure image: {filepath}")
            return filepath
            
        except Exception as e:
            logger.error(f"Failed to generate image: {e}", exc_info=True)
            return None
