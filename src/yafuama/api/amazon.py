"""Amazon SP-API endpoints: catalog search, listing management."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..amazon import AmazonApiError
from ..amazon.pricing import calculate_amazon_price, generate_sku
from ..amazon.shipping import DELIVERY_REGIONS, get_pattern_by_key, get_shipping_patterns
from ..config import settings
from ..database import get_db
from ..models import MonitoredItem
from ..schemas import (
    VALID_CONDITIONS,
    AmazonListingCreate,
    AmazonListingResponse,
    AmazonListingUpdate,
    CatalogSearchResponse,
    CatalogSearchResult,
    ListingRestriction,
    ListingRestrictionReason,
    ListingRestrictionsResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/amazon", tags=["amazon"])


def _get_sp_client():
    from ..main import app_state

    client = app_state.get("sp_api")
    if client is None:
        raise HTTPException(503, "Amazon SP-API is not configured")
    return client


def _require_item(auction_id: str, db: Session) -> MonitoredItem:
    item = db.query(MonitoredItem).filter(MonitoredItem.auction_id == auction_id).first()
    if not item:
        raise HTTPException(404, f"Item {auction_id} not found")
    return item


# --- Catalog ---


@router.get("/catalog/search", response_model=CatalogSearchResponse)
async def search_catalog(keywords: str, page_size: int = 10):
    client = _get_sp_client()
    try:
        raw_items = await client.search_catalog_items(keywords, page_size=page_size)
    except AmazonApiError as e:
        raise HTTPException(502, f"SP-API error: {e}") from e

    results = []
    for item in raw_items:
        asin = item.get("asin", "")
        summaries = item.get("summaries", [{}])
        summary = summaries[0] if summaries else {}
        images = item.get("images", [{}])
        image_list = images[0].get("images", []) if images else []
        results.append(CatalogSearchResult(
            asin=asin,
            title=summary.get("itemName", ""),
            image_url=image_list[0].get("link", "") if image_list else "",
            brand=summary.get("brand", ""),
        ))
    return CatalogSearchResponse(keywords=keywords, items=results)


@router.get("/catalog/{asin}")
async def get_catalog_item(asin: str):
    client = _get_sp_client()
    try:
        return await client.get_catalog_item(asin)
    except AmazonApiError as e:
        raise HTTPException(502, f"SP-API error: {e}") from e


# --- Listing Restrictions ---


# SP-API condition_type mapping (our internal → SP-API format)
_CONDITION_MAP = {
    "used_like_new": "used_like_new",
    "used_very_good": "used_very_good",
    "used_good": "used_good",
    "used_acceptable": "used_acceptable",
}


def _parse_restrictions(raw: list[dict]) -> list[ListingRestriction]:
    """Parse raw SP-API restriction objects into schema objects."""
    result = []
    for r in raw:
        reasons = []
        for reason in r.get("reasons", []):
            reasons.append(ListingRestrictionReason(
                reason_code=reason.get("reasonCode", ""),
                message=reason.get("message", ""),
            ))
        result.append(ListingRestriction(
            condition_type=r.get("conditionType", ""),
            is_restricted=len(reasons) > 0,
            reasons=reasons,
        ))
    return result


@router.get("/restrictions/{asin}", response_model=ListingRestrictionsResponse)
async def check_listing_restrictions(
    asin: str,
    condition: str = "used_very_good",
):
    """Check if the seller can list this ASIN (brand gating, category approval, etc.)."""
    client = _get_sp_client()
    condition_type = _CONDITION_MAP.get(condition, condition)

    raw = await client.get_listing_restrictions(asin, condition_type=condition_type)
    restrictions = _parse_restrictions(raw)
    is_listable = all(not r.is_restricted for r in restrictions) if restrictions else True

    return ListingRestrictionsResponse(
        asin=asin,
        is_listable=is_listable,
        restrictions=restrictions,
    )


# --- Listings ---


@router.post("/listings", response_model=AmazonListingResponse, status_code=201)
async def create_listing(body: AmazonListingCreate, db: Session = Depends(get_db)):
    client = _get_sp_client()
    item = _require_item(body.auction_id, db)

    if item.amazon_sku:
        raise HTTPException(409, f"Item {body.auction_id} already has Amazon listing (SKU: {item.amazon_sku})")

    condition = body.condition
    if condition not in VALID_CONDITIONS:
        raise HTTPException(400, f"Invalid condition: {condition}. Must be one of: {', '.join(VALID_CONDITIONS)}")

    # Pre-check listing restrictions (brand gating, category approval)
    if body.asin:
        condition_type = _CONDITION_MAP.get(condition, condition)
        raw_restrictions = await client.get_listing_restrictions(body.asin, condition_type=condition_type)
        restrictions = _parse_restrictions(raw_restrictions)
        blocked = [r for r in restrictions if r.is_restricted]
        if blocked:
            reasons = "; ".join(
                reason.message or reason.reason_code
                for r in blocked for reason in r.reasons
            ) or "Brand or category approval required"
            raise HTTPException(
                403,
                f"Listing restricted for ASIN {body.asin}: {reasons}",
            )

    sku = body.sku or generate_sku(body.auction_id)
    margin = body.margin_pct if body.margin_pct is not None else settings.sp_api_default_margin_pct
    shipping = body.shipping_cost or settings.sp_api_default_shipping_cost
    estimated = body.estimated_win_price or item.current_price

    price = calculate_amazon_price(estimated, shipping, margin_pct=margin)
    if price <= 0:
        raise HTTPException(400, "Calculated price is zero — check estimated_win_price")

    # Resolve shipping pattern → lead time + template name
    pattern = get_pattern_by_key(body.shipping_pattern)
    if not pattern:
        raise HTTPException(400, f"Invalid shipping_pattern: {body.shipping_pattern}")
    lead_time = pattern.lead_time_days

    try:
        attributes = {
            "condition_type": [{"value": condition}],
            "purchasable_offer": [{
                "marketplace_id": settings.sp_api_marketplace,
                "currency": "JPY",
                "our_price": [{"schedule": [{"value_with_tax": price}]}],
            }],
            "fulfillment_availability": [
                {"fulfillment_channel_code": "DEFAULT", "quantity": 1}
            ],
            "lead_time_to_ship_max_days": [{"value": lead_time}],
            "merchant_shipping_group": [{"value": pattern.template_name}],
        }
        if body.asin:
            attributes["item_name"] = [{"value": item.title, "language_tag": "ja_JP"}]

        product_type = "PRODUCT"
        await client.create_listing(settings.sp_api_seller_id, sku, product_type, attributes)
    except AmazonApiError as e:
        logger.error("Failed to create Amazon listing for %s: %s", body.auction_id, e)
        raise HTTPException(502, f"SP-API error: {e}") from e

    item.amazon_asin = body.asin
    item.amazon_sku = sku
    item.amazon_condition = condition
    item.amazon_listing_status = "active"
    item.amazon_price = price
    item.estimated_win_price = estimated
    item.shipping_cost = shipping
    item.amazon_margin_pct = margin
    item.amazon_lead_time_days = lead_time
    item.amazon_shipping_pattern = body.shipping_pattern
    item.amazon_last_synced_at = datetime.now(timezone.utc)
    item.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(item)
    return item


@router.get("/listings/{auction_id}", response_model=AmazonListingResponse)
def get_listing(auction_id: str, db: Session = Depends(get_db)):
    item = _require_item(auction_id, db)
    if not item.amazon_sku:
        raise HTTPException(404, f"Item {auction_id} has no Amazon listing")
    return item


@router.patch("/listings/{auction_id}", response_model=AmazonListingResponse)
async def update_listing(
    auction_id: str, body: AmazonListingUpdate, db: Session = Depends(get_db)
):
    client = _get_sp_client()
    item = _require_item(auction_id, db)
    if not item.amazon_sku:
        raise HTTPException(404, f"Item {auction_id} has no Amazon listing")

    if body.estimated_win_price is not None:
        item.estimated_win_price = body.estimated_win_price
    if body.shipping_cost is not None:
        item.shipping_cost = body.shipping_cost
    if body.margin_pct is not None:
        item.amazon_margin_pct = body.margin_pct
    if body.condition is not None:
        if body.condition not in VALID_CONDITIONS:
            raise HTTPException(400, f"Invalid condition: {body.condition}")
        item.amazon_condition = body.condition
    if body.lead_time_days is not None:
        item.amazon_lead_time_days = body.lead_time_days

    # Recalculate or use explicit price
    if body.amazon_price is not None:
        new_price = body.amazon_price
    else:
        new_price = calculate_amazon_price(
            item.estimated_win_price, item.shipping_cost, margin_pct=item.amazon_margin_pct
        )

    if new_price > 0 and new_price != item.amazon_price:
        try:
            await client.patch_listing_price(settings.sp_api_seller_id, item.amazon_sku, new_price)
        except AmazonApiError as e:
            logger.error("Failed to update Amazon price for %s: %s", auction_id, e)
            raise HTTPException(502, f"SP-API error: {e}") from e

    # Sync lead time to Amazon if changed
    if body.lead_time_days is not None:
        try:
            await client.patch_listing_lead_time(
                settings.sp_api_seller_id, item.amazon_sku, body.lead_time_days
            )
        except AmazonApiError as e:
            logger.error("Failed to update lead time for %s: %s", auction_id, e)
            raise HTTPException(502, f"SP-API error: {e}") from e

    item.amazon_price = new_price
    item.amazon_last_synced_at = datetime.now(timezone.utc)
    item.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/listings/{auction_id}", status_code=204)
async def delete_listing(auction_id: str, db: Session = Depends(get_db)):
    client = _get_sp_client()
    item = _require_item(auction_id, db)
    if not item.amazon_sku:
        raise HTTPException(404, f"Item {auction_id} has no Amazon listing")

    try:
        await client.delete_listing(settings.sp_api_seller_id, item.amazon_sku)
    except AmazonApiError as e:
        logger.error("Failed to delete Amazon listing for %s: %s", auction_id, e)
        raise HTTPException(502, f"SP-API error: {e}") from e

    item.amazon_sku = None
    item.amazon_asin = None
    item.amazon_listing_status = None
    item.amazon_price = None
    item.amazon_last_synced_at = None
    item.updated_at = datetime.now(timezone.utc)
    db.commit()


@router.post("/listings/{auction_id}/sync", response_model=AmazonListingResponse)
async def sync_listing(auction_id: str, db: Session = Depends(get_db)):
    """Manual sync: set quantity based on current auction status."""
    client = _get_sp_client()
    item = _require_item(auction_id, db)
    if not item.amazon_sku:
        raise HTTPException(404, f"Item {auction_id} has no Amazon listing")

    quantity = 0 if (item.status or "").startswith("ended_") else 1
    try:
        await client.patch_listing_quantity(settings.sp_api_seller_id, item.amazon_sku, quantity)
    except AmazonApiError as e:
        logger.error("Failed to sync Amazon listing for %s: %s", auction_id, e)
        item.amazon_listing_status = "error"
        db.commit()
        raise HTTPException(502, f"SP-API error: {e}") from e

    item.amazon_listing_status = "inactive" if quantity == 0 else "active"
    item.amazon_last_synced_at = datetime.now(timezone.utc)
    item.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(item)
    return item


# --- Shipping Patterns ---


@router.get("/shipping-patterns")
def list_shipping_patterns():
    patterns = get_shipping_patterns()
    return {
        "patterns": [
            {"key": p.key, "label": p.label, "lead_time_days": p.lead_time_days}
            for p in patterns
        ],
        "delivery_regions": [
            {"region": r.region, "areas": r.areas, "days": r.days}
            for r in DELIVERY_REGIONS
        ],
    }
