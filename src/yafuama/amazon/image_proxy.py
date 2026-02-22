"""Download Yahoo auction images and upload to S3 for Amazon SP-API access."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from functools import partial
from io import BytesIO
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

_s3_client: Any = None


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        import boto3

        _s3_client = boto3.client(
            "s3",
            region_name=settings.s3_image_region,
            aws_access_key_id=settings.sp_api_aws_access_key,
            aws_secret_access_key=settings.sp_api_aws_secret_key,
        )
    return _s3_client


def _upload_to_s3(image_bytes: bytes, key: str, content_type: str) -> str:
    """Upload image bytes to S3 and return the public URL."""
    client = _get_s3_client()
    client.put_object(
        Bucket=settings.s3_image_bucket,
        Key=key,
        Body=image_bytes,
        ContentType=content_type,
        CacheControl="public, max-age=2592000",  # 30 days
    )
    return f"https://{settings.s3_image_bucket}.s3.{settings.s3_image_region}.amazonaws.com/{key}"


async def upload_images_to_s3(
    image_urls: list[str],
    auction_id: str,
) -> list[str]:
    """Download images from Yahoo CDN and upload to S3.

    Returns a list of S3 public URLs (same order as input).
    If S3 is not configured, returns the original URLs unchanged.
    """
    if not settings.s3_image_enabled:
        return image_urls

    if not image_urls:
        return []

    s3_urls: list[str] = []
    loop = asyncio.get_event_loop()

    async with httpx.AsyncClient(
        timeout=15.0,
        headers={"User-Agent": settings.scraper_user_agent},
    ) as http:
        for i, url in enumerate(image_urls):
            try:
                resp = await http.get(url)
                resp.raise_for_status()
                image_bytes = resp.content

                # Determine content type
                ct = resp.headers.get("content-type", "image/jpeg")
                ext = "jpg"
                if "png" in ct:
                    ext = "png"
                elif "webp" in ct:
                    ext = "webp"

                # Unique key: auction_id + image index + content hash
                content_hash = hashlib.md5(image_bytes).hexdigest()[:8]
                key = f"offer-images/{auction_id}/{i:02d}_{content_hash}.{ext}"

                # Upload (blocking boto3 call in executor)
                s3_url = await loop.run_in_executor(
                    None,
                    partial(_upload_to_s3, image_bytes, key, ct),
                )
                s3_urls.append(s3_url)
                logger.debug("Uploaded image %d for %s → %s", i, auction_id, s3_url)

            except Exception as e:
                logger.warning(
                    "Failed to proxy image %d for %s: %s — using original URL",
                    i, auction_id, e,
                )
                # Fallback: use original Yahoo URL
                s3_urls.append(url)

    logger.info(
        "Proxied %d/%d images for %s to S3",
        sum(1 for u in s3_urls if "s3." in u), len(image_urls), auction_id,
    )
    return s3_urls
