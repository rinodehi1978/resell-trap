"""Shared Amazon listing submission logic.

All three listing code paths (list_from_deal, create_listing, relist_listing)
funnel through submit_to_amazon() for the SP-API call sequence:
  PUT → 3s wait → condition_note PATCH → image PATCH → price PATCH
  → quantity PATCH → price Feed → inventory Feed

This prevents PATCH omissions (e.g. condition_note not sent separately
from PUT in LISTING_OFFER_ONLY mode).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from . import AmazonApiError

logger = logging.getLogger(__name__)


@dataclass
class ListingParams:
    """Parameters for submit_to_amazon(). Built by each caller."""

    seller_id: str
    sku: str
    asin: str
    price: int
    condition: str
    lead_time: int
    shipping_template: str  # merchant_shipping_group value
    product_type: str = "PRODUCT"
    condition_note: str = ""
    image_urls: list[str] = field(default_factory=list)
    auction_id: str = ""  # for S3 image proxy naming


@dataclass
class ListingResult:
    """Result from submit_to_amazon()."""

    success: bool
    sku: str
    s3_image_urls: list[str] = field(default_factory=list)


async def submit_to_amazon(
    sp_client,
    params: ListingParams,
) -> ListingResult:
    """Execute the full SP-API listing sequence.

    1. PUT (LISTING_OFFER_ONLY)
    2. Wait 3 seconds
    3. PATCH condition_note (PUT ignores this in offer-only mode)
    4. PATCH offer images (via S3 proxy)
    5. PATCH price + quantity (activation)
    6. Submit price + inventory Feeds (Seller Central sync)

    Raises AmazonApiError only for critical failures (PUT rejected).
    PATCH/Feed failures are logged but non-fatal.
    """
    from ..config import settings

    seller_id = params.seller_id
    sku = params.sku

    # --- 1. Build attributes and PUT ---
    attributes = {
        "condition_type": [{"value": params.condition}],
        "purchasable_offer": [{
            "marketplace_id": settings.sp_api_marketplace,
            "currency": "JPY",
            "our_price": [{"schedule": [{"value_with_tax": params.price}]}],
        }],
        "fulfillment_availability": [
            {"fulfillment_channel_code": "DEFAULT", "quantity": 1}
        ],
        "merchant_suggested_asin": [{
            "value": params.asin,
            "marketplace_id": settings.sp_api_marketplace,
        }],
        "merchant_shipping_group": [{"value": params.shipping_template}],
        "lead_time_to_ship_max_days": [{"value": params.lead_time}],
    }

    # condition_note in PUT attributes (may be ignored in offer-only mode,
    # but included for non-offer-only compatibility; PATCH below is the real fix)
    if params.condition_note:
        attributes["condition_note"] = [
            {"value": params.condition_note, "language_tag": "ja_JP"}
        ]

    try:
        await sp_client.create_listing(
            seller_id, sku, params.product_type, attributes,
            offer_only=True,
        )
    except AmazonApiError as e:
        if "INVALID" in str(e):
            logger.info("PUT INVALID with lead_time for %s, retrying without", sku)
            attributes.pop("lead_time_to_ship_max_days", None)
            await sp_client.create_listing(
                seller_id, sku, params.product_type, attributes,
                offer_only=True,
            )
            try:
                await sp_client.patch_listing_lead_time(seller_id, sku, params.lead_time)
            except AmazonApiError:
                logger.info("lead_time PATCH also failed for %s", sku)
        else:
            raise

    # --- 2. Wait for PUT to propagate ---
    await asyncio.sleep(3)

    # --- 3. PATCH condition_note (LISTING_OFFER_ONLY ignores it in PUT) ---
    if params.condition_note:
        try:
            await sp_client.patch_condition_note(seller_id, sku, params.condition_note)
            logger.info("Condition note PATCH sent for %s", sku)
        except AmazonApiError:
            logger.warning("Condition note PATCH failed for %s (non-critical)", sku)

    # --- 4. PATCH offer images (S3 proxy) ---
    s3_image_urls: list[str] = []
    if params.image_urls:
        from .image_proxy import upload_images_to_s3

        s3_image_urls = await upload_images_to_s3(
            params.image_urls, params.auction_id or sku,
        )
        s3_count = sum(1 for u in s3_image_urls if "s3." in u)
        logger.info(
            "Image proxy: %d/%d uploaded to S3 for %s, URLs: %s",
            s3_count, len(s3_image_urls), sku, s3_image_urls,
        )
        try:
            await sp_client.patch_offer_images(seller_id, sku, s3_image_urls)
            logger.info("Offer image PATCH sent for %s (%d images)", sku, len(s3_image_urls))
        except AmazonApiError as e:
            logger.error("Offer image PATCH failed for %s: %s", sku, e)

    # --- 5. PATCH price + quantity (activation) ---
    try:
        await sp_client.patch_listing_price(seller_id, sku, params.price)
        logger.info("Price PATCH sent for %s (¥%d)", sku, params.price)
    except AmazonApiError:
        logger.warning("Price PATCH failed for %s", sku)

    try:
        await sp_client.patch_listing_quantity(seller_id, sku, 1)
        logger.info("Quantity PATCH sent for %s", sku)
    except AmazonApiError:
        logger.warning("Quantity PATCH failed for %s", sku)

    # --- 6. Feeds (Seller Central sync) ---
    try:
        await sp_client.submit_price_feed(seller_id, sku, params.price)
        logger.info("Price feed submitted for %s (¥%d)", sku, params.price)
    except AmazonApiError:
        logger.warning("Price feed failed for %s (non-critical)", sku)

    try:
        await sp_client.submit_inventory_feed(seller_id, sku, 1, params.lead_time)
        logger.info("Inventory feed submitted for %s", sku)
    except AmazonApiError:
        logger.warning("Inventory feed failed for %s (non-critical)", sku)

    return ListingResult(
        success=True,
        sku=sku,
        s3_image_urls=s3_image_urls,
    )
