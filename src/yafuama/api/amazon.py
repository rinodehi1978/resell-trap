"""Amazon SP-API endpoints: catalog search, listing management."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..amazon import AmazonApiError
from ..amazon.listing import ListingParams, submit_to_amazon
from ..amazon.pricing import calculate_amazon_price, generate_sku
from ..amazon.shipping import DELIVERY_REGIONS, get_pattern_by_key, get_shipping_patterns
from ..config import settings
from ..database import get_db
from ..models import MonitoredItem, StatusHistory
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
    if not keywords or not keywords.strip():
        raise HTTPException(400, "キーワードを入力してください")
    if page_size < 1 or page_size > 100:
        raise HTTPException(400, "page_sizeは1〜100の範囲で指定してください")
    keywords = keywords.strip()
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
    if not asin or len(asin) < 10:
        raise HTTPException(400, "正しいASINを入力してください")
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
    if not asin or len(asin) < 10:
        raise HTTPException(400, "正しいASINを入力してください")
    if condition not in _CONDITION_MAP:
        raise HTTPException(400, f"無効なコンディション: {condition}")
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
    forwarding = getattr(body, "forwarding_cost", 0) or item.forwarding_cost or settings.deal_forwarding_cost
    estimated = body.estimated_win_price or item.current_price

    price = calculate_amazon_price(estimated, shipping, forwarding_cost=forwarding, margin_pct=margin)
    if price <= 0:
        raise HTTPException(400, "Calculated price is zero — check estimated_win_price")

    # Resolve shipping pattern → lead time + template name
    pattern = get_pattern_by_key(body.shipping_pattern)
    if not pattern:
        raise HTTPException(400, f"Invalid shipping_pattern: {body.shipping_pattern}")
    lead_time = pattern.lead_time_days

    try:
        if body.asin:
            # Offer on existing ASIN: use shared submit_to_amazon()
            product_type = await client.get_product_type(body.asin)
            result = await submit_to_amazon(
                client,
                ListingParams(
                    seller_id=settings.sp_api_seller_id,
                    sku=sku,
                    asin=body.asin,
                    price=price,
                    condition=condition,
                    lead_time=lead_time,
                    shipping_template=pattern.template_name,
                    product_type=product_type,
                    condition_note=body.condition_note or "",
                    image_urls=body.image_urls or [],
                    auction_id=body.auction_id,
                ),
            )
        else:
            # New product (no ASIN): simple PUT with images in attributes
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
            if body.condition_note:
                attributes["condition_note"] = [
                    {"value": body.condition_note, "language_tag": "ja_JP"}
                ]
            if body.image_urls:
                attributes["main_offer_image_locator"] = [
                    {"media_location": body.image_urls[0]}
                ]
                for i, url in enumerate(body.image_urls[1:6]):
                    attributes[f"other_offer_image_locator_{i + 1}"] = [
                        {"media_location": url}
                    ]
            await client.create_listing(
                settings.sp_api_seller_id, sku, "PRODUCT", attributes,
            )
            result = None
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
    item.forwarding_cost = forwarding
    item.amazon_margin_pct = margin
    item.amazon_lead_time_days = lead_time
    item.amazon_shipping_pattern = body.shipping_pattern
    item.amazon_condition_note = body.condition_note
    if body.image_urls:
        # Save S3-proxied URLs if available, otherwise original URLs
        s3_urls = result.s3_image_urls if result else None
        item.amazon_image_urls = json.dumps(s3_urls or body.image_urls)
    item.amazon_last_synced_at = datetime.now(timezone.utc)
    item.updated_at = datetime.now(timezone.utc)
    item.seller_central_checklist = ""  # チェックリストリセット

    # 履歴に記録
    db.add(StatusHistory(
        item_id=item.id, auction_id=item.auction_id,
        change_type="amazon_listing",
        new_status=sku,
    ))
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
            item.estimated_win_price, item.shipping_cost,
            forwarding_cost=item.forwarding_cost or 0,
            margin_pct=item.amazon_margin_pct,
            amazon_fee_pct=item.amazon_fee_pct,
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

    old_sku = item.amazon_sku
    item.amazon_sku = None
    item.amazon_listing_status = "delisted"
    item.amazon_last_synced_at = None
    item.updated_at = datetime.now(timezone.utc)

    # 履歴に記録
    db.add(StatusHistory(
        item_id=item.id, auction_id=item.auction_id,
        change_type="amazon_delist",
        old_status=old_sku,
    ))
    db.commit()


@router.patch("/listings/{auction_id}/price")
async def update_listing_price(auction_id: str, body: dict, db: Session = Depends(get_db)):
    """Update Amazon listing price via SP-API and sync DB.

    If the item is delisted (no SKU), only updates the DB price
    so the user can adjust it before re-listing.
    """
    item = _require_item(auction_id, db)

    new_price = body.get("price")
    if not new_price or not isinstance(new_price, (int, float)) or new_price <= 0:
        raise HTTPException(400, "正しい価格を入力してください")
    new_price = int(new_price)

    # If listing is active (has SKU), also update via SP-API
    if item.amazon_sku:
        client = _get_sp_client()
        try:
            await client.patch_listing_price(settings.sp_api_seller_id, item.amazon_sku, new_price)
        except AmazonApiError as e:
            raise HTTPException(502, f"価格変更に失敗: {e}") from e

        # Feeds API: セラーセントラルの在庫管理システムにも価格を反映
        try:
            await client.submit_price_feed(settings.sp_api_seller_id, item.amazon_sku, new_price)
            logger.info("Price feed submitted for %s (¥%d)", item.amazon_sku, new_price)
        except AmazonApiError:
            logger.warning("Price feed failed for %s (non-critical)", item.amazon_sku)
    elif not item.amazon_asin:
        raise HTTPException(404, "Amazon出品情報がありません")

    old_price = item.amazon_price
    item.amazon_price = new_price
    item.amazon_last_synced_at = datetime.now(timezone.utc) if item.amazon_sku else item.amazon_last_synced_at
    item.updated_at = datetime.now(timezone.utc)

    db.add(StatusHistory(
        item_id=item.id, auction_id=item.auction_id,
        change_type="price_change",
        old_price=old_price, new_price=new_price,
    ))
    db.commit()

    return {"status": "ok", "old_price": old_price, "new_price": new_price}


@router.post("/listings/{auction_id}/relist", response_model=AmazonListingResponse, status_code=201)
async def relist_listing(auction_id: str, body: dict = None, db: Session = Depends(get_db)):
    """Re-create Amazon listing using stored data (after delist)."""
    client = _get_sp_client()
    item = _require_item(auction_id, db)
    body = body or {}

    if item.amazon_sku:
        raise HTTPException(409, f"Item {auction_id} already has an active listing (SKU: {item.amazon_sku})")
    if not item.amazon_asin:
        raise HTTPException(400, f"Item {auction_id} has no ASIN — use Deal page to list")
    if (item.status or "").startswith("ended_"):
        raise HTTPException(410, "ヤフオクが終了済みのため再出品できません。新しいDealから出品してください。")

    condition = body.get("condition") or item.amazon_condition or "used_very_good"
    if condition not in VALID_CONDITIONS:
        raise HTTPException(400, f"Invalid condition: {condition}")
    # Unique SKU to avoid reuse issues after Seller Central deletion
    suffix = datetime.now(timezone.utc).strftime("%y%m%d%H%M")
    sku = f"{generate_sku(auction_id)}-R{suffix}"

    # body で仕入れ価格・送料が更新された場合、itemにも反映
    if body.get("estimated_win_price"):
        item.estimated_win_price = body["estimated_win_price"]
    if "shipping_cost" in body:
        item.shipping_cost = body["shipping_cost"]

    price = body.get("price") or item.amazon_price or calculate_amazon_price(
        item.estimated_win_price, item.shipping_cost,
        forwarding_cost=item.forwarding_cost or 0,
        margin_pct=item.amazon_margin_pct,
        amazon_fee_pct=item.amazon_fee_pct,
    )
    if price <= 0:
        raise HTTPException(400, "Price is zero — check estimated_win_price")

    shipping_pattern_key = body.get("shipping_pattern") or item.amazon_shipping_pattern or "2_3_days"
    pattern = get_pattern_by_key(shipping_pattern_key)
    if not pattern:
        pattern = get_pattern_by_key("2_3_days")
    lead_time = pattern.lead_time_days

    # condition_note: bodyで上書き可能（コンディション変更時にテンプレート差し替え）
    condition_note = body.get("condition_note") or item.amazon_condition_note or ""

    # Resolve image URLs: body → stored DB → scrape Yahoo auction page
    image_urls = body.get("image_urls", [])
    if not image_urls and item.amazon_image_urls:
        try:
            image_urls = json.loads(item.amazon_image_urls)
        except (ValueError, TypeError):
            image_urls = []
    if not image_urls:
        from ..main import app_state
        scraper = app_state.get("scraper")
        if scraper:
            try:
                image_urls = await scraper.fetch_auction_images(item.auction_id)
                logger.info("Relist fallback: scraped %d images from Yahoo for %s", len(image_urls), item.auction_id)
            except Exception:
                logger.warning("Relist fallback: failed to scrape images for %s", item.auction_id)

    try:
        product_type = await client.get_product_type(item.amazon_asin)
        result = await submit_to_amazon(
            client,
            ListingParams(
                seller_id=settings.sp_api_seller_id,
                sku=sku,
                asin=item.amazon_asin,
                price=price,
                condition=condition,
                lead_time=lead_time,
                shipping_template=pattern.template_name,
                product_type=product_type,
                condition_note=condition_note,
                image_urls=image_urls,
                auction_id=item.auction_id,
            ),
        )
    except AmazonApiError as e:
        logger.error("Failed to relist Amazon for %s: %s", auction_id, e)
        raise HTTPException(502, f"SP-API error: {e}") from e

    item.amazon_sku = sku
    item.amazon_condition = condition
    item.amazon_listing_status = "active"
    item.amazon_price = price
    item.amazon_lead_time_days = lead_time
    item.amazon_shipping_pattern = shipping_pattern_key
    if condition_note:
        item.amazon_condition_note = condition_note
    # Save S3-proxied image URLs for future relist persistence
    if result.s3_image_urls:
        item.amazon_image_urls = json.dumps(result.s3_image_urls)
    elif body.get("image_urls"):
        item.amazon_image_urls = json.dumps(body["image_urls"])
    item.amazon_last_synced_at = datetime.now(timezone.utc)
    item.updated_at = datetime.now(timezone.utc)
    item.seller_central_checklist = ""

    db.add(StatusHistory(
        item_id=item.id, auction_id=item.auction_id,
        change_type="amazon_listing",
        new_status=sku,
    ))
    db.commit()
    db.refresh(item)
    return item


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
