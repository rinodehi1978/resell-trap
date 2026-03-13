"""Product image comparison using Claude Vision API."""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Amazon image CDN base (Keepa imagesCSV uses filename only)
_AMAZON_IMAGE_CDN = "https://m.media-amazon.com/images/I/"


def keepa_image_url(images_csv: str) -> str | None:
    """Extract the main product image URL from Keepa imagesCSV field.

    imagesCSV format: "51abc123._SL1000_.jpg,41def456._SL1000_.jpg,..."
    Returns the first (main) image URL or None.
    """
    if not images_csv:
        return None
    first = images_csv.split(",")[0].strip()
    if not first:
        return None
    return f"{_AMAZON_IMAGE_CDN}{first}"


def sp_api_main_image_url(catalog_item: dict) -> str | None:
    """Extract main image URL from SP-API getCatalogItem response."""
    image_sets = catalog_item.get("images", [])
    for image_set in image_sets:
        for img in image_set.get("images", []):
            if img.get("variant") == "MAIN" and img.get("link"):
                return img["link"]
    # Fallback: first image
    for image_set in image_sets:
        images = image_set.get("images", [])
        if images and images[0].get("link"):
            return images[0]["link"]
    return None


class ImageVerifier:
    """Compare product images using Claude Vision API (Haiku 4.5)."""

    def __init__(self, api_key: str, model: str = "claude-haiku-4-5-20251001") -> None:
        # Lazy import to avoid requiring anthropic when vision is disabled
        import anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model
        self._http = httpx.AsyncClient(timeout=15.0, follow_redirects=True)

    async def compare_images(
        self,
        image_url_a: str,
        image_url_b: str,
    ) -> bool | None:
        """Compare two product images.

        Returns:
            True: Same product (same model, color, edition)
            False: Different product
            None: Could not determine (fetch/API error) — caller should allow through
        """
        img_a = await self._fetch_image(image_url_a)
        img_b = await self._fetch_image(image_url_b)
        if img_a is None or img_b is None:
            logger.warning("Image fetch failed — skipping verification")
            return None

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=100,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": img_a[1],
                                "data": img_a[0],
                            },
                        },
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": img_b[1],
                                "data": img_b[0],
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "これは同じ商品ですか？メーカー・型番・色・エディションが同じなら YES、"
                                "違う商品やカラー違い・エディション違いなら NO と答えてください。"
                                "最初の1単語で YES または NO のみ回答してください。"
                            ),
                        },
                    ],
                }],
            )
        except Exception as e:
            logger.warning("Vision API error: %s", e)
            return None

        answer = response.content[0].text.strip().upper()
        is_same = answer.startswith("YES")
        logger.info(
            "Vision compare: %s (answer=%s)",
            "SAME" if is_same else "DIFFERENT",
            answer[:20],
        )
        return is_same

    async def find_matching_variation(
        self,
        yahoo_image_url: str,
        variation_images: list[tuple[str, str]],
    ) -> str | None:
        """Find a matching ASIN from variation images.

        Args:
            yahoo_image_url: Yahoo auction image URL
            variation_images: List of (asin, image_url) tuples

        Returns:
            Matching ASIN or None
        """
        for asin, img_url in variation_images:
            result = await self.compare_images(yahoo_image_url, img_url)
            if result is True:
                logger.info("Variation match found: ASIN %s", asin)
                return asin
        return None

    async def _fetch_image(self, url: str) -> tuple[str, str] | None:
        """Fetch image and return (base64_data, media_type) or None."""
        try:
            resp = await self._http.get(url)
            if resp.status_code != 200:
                logger.debug("Image fetch %d: %s", resp.status_code, url[:80])
                return None
            content_type = resp.headers.get("content-type", "image/jpeg")
            if "jpeg" in content_type or "jpg" in content_type:
                media_type = "image/jpeg"
            elif "png" in content_type:
                media_type = "image/png"
            elif "webp" in content_type:
                media_type = "image/webp"
            elif "gif" in content_type:
                media_type = "image/gif"
            else:
                media_type = "image/jpeg"  # default

            data = base64.standard_b64encode(resp.content).decode("ascii")
            return (data, media_type)
        except Exception as e:
            logger.debug("Image fetch error (%s): %s", url[:60], e)
            return None

    async def close(self) -> None:
        await self._http.aclose()
