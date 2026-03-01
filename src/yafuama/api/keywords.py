"""Watched keywords CRUD and manual scan trigger."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ..database import get_db
from ..matcher import is_apparel
from ..models import DealAlert, MonitoredItem, StatusHistory, WatchedKeyword
from ..schemas import (
    DealAlertListResponse,
    WatchedKeywordCreate,
    WatchedKeywordListResponse,
    WatchedKeywordResponse,
    WatchedKeywordUpdate,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/keywords", tags=["keywords"])


def _kw_to_response(kw: WatchedKeyword, db: Session) -> dict:
    """Convert a WatchedKeyword to response dict with alert_count."""
    count = db.query(DealAlert).filter(DealAlert.keyword_id == kw.id).count()
    return WatchedKeywordResponse(
        id=kw.id,
        keyword=kw.keyword,
        is_active=kw.is_active,
        last_scanned_at=kw.last_scanned_at,
        created_at=kw.created_at,
        updated_at=kw.updated_at,
        notes=kw.notes,
        alert_count=count,
        source=kw.source,
        parent_keyword_id=kw.parent_keyword_id,
        performance_score=kw.performance_score,
        total_scans=kw.total_scans,
        total_deals_found=kw.total_deals_found,
        confidence=kw.confidence,
        auto_deactivated_at=kw.auto_deactivated_at,
    )


# ---------------------------------------------------------------------------
# Keyword CRUD (no path params that could conflict)
# ---------------------------------------------------------------------------

@router.get("", response_model=WatchedKeywordListResponse)
def list_keywords(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(WatchedKeyword).order_by(WatchedKeyword.created_at.desc())
    total = q.count()
    keywords = q.offset(offset).limit(limit).all()
    items = [_kw_to_response(kw, db) for kw in keywords]
    return WatchedKeywordListResponse(keywords=items, total=total)


@router.post("", response_model=WatchedKeywordResponse, status_code=201)
def create_keyword(body: WatchedKeywordCreate, db: Session = Depends(get_db)):
    keyword = body.keyword.strip()
    if not keyword:
        raise HTTPException(400, "Keyword must not be empty")

    if is_apparel(keyword):
        raise HTTPException(400, "アパレル関連キーワードは登録できません")

    existing = db.query(WatchedKeyword).filter(WatchedKeyword.keyword == keyword).first()
    if existing:
        raise HTTPException(409, f"Keyword '{keyword}' already exists")

    kw = WatchedKeyword(keyword=keyword, is_active=body.is_active, notes=body.notes)
    db.add(kw)
    db.commit()
    db.refresh(kw)
    return _kw_to_response(kw, db)


# ---------------------------------------------------------------------------
# Alert routes — MUST be registered BEFORE /{keyword_id} routes
# to prevent "alerts" from being parsed as keyword_id int param.
# ---------------------------------------------------------------------------

VALID_REJECTION_REASONS = (
    "wrong_product",   # 商品違い
    "accessory",       # 部品/アクセサリー
    "model_variant",   # モデル/バリアント違い
    "bad_price",       # 価格おかしい
    "never_show",      # 二度と出すな（同タイトルペアをブロック）
    "other",           # その他
)


@router.get("/alerts/{alert_id}/images")
async def get_alert_images(alert_id: int, db: Session = Depends(get_db)):
    """Fetch all product images from the Yahoo auction page for this alert."""
    alert = db.query(DealAlert).filter(DealAlert.id == alert_id).first()
    if not alert:
        raise HTTPException(404, "Alert not found")

    from ..main import app_state

    scraper = app_state.get("scraper")
    if not scraper:
        raise HTTPException(503, "Scraper not available")

    images = await scraper.fetch_auction_images(alert.yahoo_auction_id)
    return {"images": images, "auction_id": alert.yahoo_auction_id}


@router.get("/alerts/{alert_id}/suggest-rejection")
def suggest_rejection_reasons(alert_id: int, db: Session = Depends(get_db)):
    """Analyze an alert's titles and suggest specific rejection reasons."""
    alert = db.query(DealAlert).filter(DealAlert.id == alert_id).first()
    if not alert:
        raise HTTPException(404, "Alert not found")

    from ..ai.rejection_analyzer import suggest_reasons

    suggestions = suggest_reasons(alert, db)
    static_reasons = [
        {"reason": "wrong_product", "label": "商品違い"},
        {"reason": "accessory", "label": "部品/アクセサリー"},
        {"reason": "model_variant", "label": "モデル/バリアント違い"},
        {"reason": "bad_price", "label": "価格おかしい"},
        {"reason": "never_show", "label": "二度と出すな"},
        {"reason": "other", "label": "その他"},
    ]
    return {"suggestions": suggestions, "static_reasons": static_reasons}


@router.post("/alerts/{alert_id}/reject", status_code=200)
def reject_alert(alert_id: int, body: dict = None, db: Session = Depends(get_db)):
    """Soft-delete a deal alert with rejection reason for learning."""
    alert = db.query(DealAlert).filter(DealAlert.id == alert_id).first()
    if not alert:
        raise HTTPException(404, "Alert not found")

    body = body or {}
    reason = body.get("reason", "other")
    if reason not in VALID_REJECTION_REASONS:
        reason = "other"

    alert.status = "rejected"
    alert.rejection_reason = reason
    alert.rejection_note = body.get("note", "")
    alert.rejected_at = datetime.now(timezone.utc)

    # Analyze rejection and persist learned patterns
    try:
        from ..ai.rejection_analyzer import analyze_single_rejection
        analyze_single_rejection(alert, reason, db)
    except Exception:
        pass  # Non-critical: don't fail the rejection itself

    for attempt in range(3):
        try:
            db.commit()
            break
        except OperationalError:
            db.rollback()
            if attempt < 2:
                time.sleep(1)
                # Re-apply changes after rollback
                alert.status = "rejected"
                alert.rejection_reason = reason
                alert.rejection_note = body.get("note", "")
                alert.rejected_at = datetime.now(timezone.utc)
            else:
                logger.warning("DB locked during reject after 3 attempts")
                raise HTTPException(503, "データベースが一時的にビジーです。再試行してください。")

    # Reload matcher overrides with new patterns
    try:
        from ..matcher_overrides import overrides
        overrides.reload()
    except Exception:
        pass

    return {"ok": True, "id": alert_id, "reason": reason}


@router.delete("/alerts/{alert_id}", status_code=204)
def delete_alert(alert_id: int, db: Session = Depends(get_db)):
    """Hard-delete a deal alert (fallback)."""
    alert = db.query(DealAlert).filter(DealAlert.id == alert_id).first()
    if not alert:
        raise HTTPException(404, "Alert not found")
    db.delete(alert)
    for attempt in range(3):
        try:
            db.commit()
            return
        except OperationalError:
            db.rollback()
            if attempt < 2:
                time.sleep(1)
                # Re-attach alert for deletion after rollback
                alert = db.query(DealAlert).filter(DealAlert.id == alert_id).first()
                if not alert:
                    return  # Already deleted by another process
                db.delete(alert)
            else:
                logger.warning("DB locked during delete after 3 attempts")
                raise HTTPException(503, "データベースが一時的にビジーです。再試行してください。")


@router.post("/alerts/{alert_id}/list", status_code=201)
async def list_from_deal(
    alert_id: int,
    body: dict = None,
    db: Session = Depends(get_db),
):
    """Create MonitoredItem + Amazon listing from a deal alert.

    Expects: {
        "condition": "used_very_good",
        "price": 9800,
        "condition_note": "動作確認済み。...",
        "image_urls": ["https://..."],
        "shipping_pattern": "2_3_days"
    }
    """
    from ..amazon.pricing import calculate_amazon_price, generate_sku
    from ..amazon.shipping import get_pattern_by_key
    from ..config import settings
    from ..main import app_state
    from ..schemas import VALID_CONDITIONS

    alert = db.query(DealAlert).filter(DealAlert.id == alert_id).first()
    if not alert:
        raise HTTPException(404, "Alert not found")

    # ヤフオクが落札済み/終了済みでないか確認
    scraper = app_state.get("scraper")
    if scraper:
        auction_data = await scraper.fetch_auction(alert.yahoo_auction_id)
        if auction_data and auction_data.is_closed:
            status_label = "落札済み" if auction_data.has_winner else "終了（落札者なし）"
            raise HTTPException(
                410, f"このヤフオクは既に{status_label}です。出品できません。"
            )

    body = body or {}
    condition = body.get("condition", "used_very_good")
    if condition not in VALID_CONDITIONS:
        raise HTTPException(400, f"Invalid condition: {condition}")

    # Check if MonitoredItem already exists
    existing = db.query(MonitoredItem).filter(
        MonitoredItem.auction_id == alert.yahoo_auction_id
    ).first()
    if existing and existing.amazon_sku:
        raise HTTPException(409, "この商品は既にAmazonに出品済みです")

    # Create or reuse MonitoredItem
    if existing:
        item = existing
    else:
        item = MonitoredItem(
            auction_id=alert.yahoo_auction_id,
            title=alert.yahoo_title,
            url=alert.yahoo_url or f"https://auctions.yahoo.co.jp/jp/auction/{alert.yahoo_auction_id}",
            image_url=alert.yahoo_image_url or "",
            current_price=alert.yahoo_price,
            buy_now_price=alert.yahoo_price,
        )
        db.add(item)
        db.flush()  # Get item.id

    # Calculate price
    yahoo_shipping = getattr(alert, "yahoo_shipping", 0) or 0
    estimated_price = alert.yahoo_price
    shipping = yahoo_shipping or settings.sp_api_default_shipping_cost
    margin = settings.sp_api_default_margin_pct
    amazon_price = body.get("price") or calculate_amazon_price(
        estimated_price, shipping, margin_pct=margin
    )

    # Create Amazon listing via SP-API
    sp_client = app_state.get("sp_api")
    if not sp_client:
        raise HTTPException(503, "Amazon SP-APIが未設定です")

    sku = generate_sku(alert.yahoo_auction_id)

    # Resolve shipping pattern → lead time + template name
    shipping_pattern_key = body.get("shipping_pattern", "2_3_days")
    pattern = get_pattern_by_key(shipping_pattern_key)
    if not pattern:
        raise HTTPException(400, f"Invalid shipping_pattern: {shipping_pattern_key}")
    lead_time = pattern.lead_time_days

    try:
        from ..amazon import AmazonApiError

        # Check restrictions
        raw_restrictions = await sp_client.get_listing_restrictions(
            alert.amazon_asin, condition_type=condition
        )
        blocked = [r for r in raw_restrictions if r.get("reasons")]
        if blocked:
            reasons = "; ".join(
                reason.get("message", reason.get("reasonCode", ""))
                for r in blocked for reason in r.get("reasons", [])
            ) or "ブランド承認またはカテゴリ承認が必要です"
            raise HTTPException(403, f"出品制限: {reasons}")

        attributes = {
            "condition_type": [{"value": condition}],
            "purchasable_offer": [{
                "marketplace_id": settings.sp_api_marketplace,
                "currency": "JPY",
                "our_price": [{"schedule": [{"value_with_tax": amazon_price}]}],
            }],
            "fulfillment_availability": [
                {"fulfillment_channel_code": "DEFAULT", "quantity": 1}
            ],
            # Offer on existing ASIN: use LISTING_OFFER_ONLY mode
            "merchant_suggested_asin": [{
                "value": alert.amazon_asin,
                "marketplace_id": settings.sp_api_marketplace,
            }],
            "merchant_shipping_group": [{"value": pattern.template_name}],
            "lead_time_to_ship_max_days": [{"value": lead_time}],
        }

        # 提供条件に関する注記（出品情報ページ、中古コンディション説明）
        condition_note = body.get("condition_note", "").strip()
        if condition_note:
            attributes["condition_note"] = [
                {"value": condition_note, "language_tag": "ja_JP"}
            ]

        # Get correct productType from Catalog API
        product_type = await sp_client.get_product_type(alert.amazon_asin)

        # Try with lead_time; if INVALID, retry without it
        try:
            await sp_client.create_listing(
                settings.sp_api_seller_id, sku, product_type, attributes,
                offer_only=True,
            )
        except AmazonApiError as e:
            if "INVALID" in str(e):
                logger.info("Listing INVALID with lead_time for %s, retrying without", sku)
                attributes.pop("lead_time_to_ship_max_days", None)
                await sp_client.create_listing(
                    settings.sp_api_seller_id, sku, product_type, attributes,
                    offer_only=True,
                )
                # Fallback: try PATCH for lead_time
                try:
                    await sp_client.patch_listing_lead_time(
                        settings.sp_api_seller_id, sku, lead_time,
                    )
                except AmazonApiError:
                    logger.info("lead_time PATCH also failed for %s", sku)
            else:
                raise

        # Wait for PUT to be processed before sending PATCHes
        import asyncio
        await asyncio.sleep(3)

        # Offer images: send separately via PATCH after listing is created
        # to prevent image issues from causing "不完全" status
        image_urls = body.get("image_urls", [])
        if image_urls:
            # S3プロキシ: Yahoo CDN画像をS3にアップロードしてからAmazonに送信
            from ..amazon.image_proxy import upload_images_to_s3

            image_urls = await upload_images_to_s3(image_urls, alert.yahoo_auction_id)
            try:
                await sp_client.patch_offer_images(
                    settings.sp_api_seller_id, sku, image_urls,
                )
            except AmazonApiError:
                logger.warning("Offer image PATCH failed for %s (non-critical)", sku)

        # PATCH price + quantity to trigger listing activation
        # (PUT alone creates the record but doesn't activate the offer;
        #  some product types need explicit quantity PATCH to become BUYABLE)
        try:
            await sp_client.patch_listing_price(
                settings.sp_api_seller_id, sku, amazon_price,
            )
            logger.info("Activation price PATCH sent for %s (¥%d)", sku, amazon_price)
        except AmazonApiError:
            logger.warning("Activation price PATCH failed for %s", sku)
        try:
            await sp_client.patch_listing_quantity(
                settings.sp_api_seller_id, sku, 1,
            )
            logger.info("Activation quantity PATCH sent for %s", sku)
        except AmazonApiError:
            logger.warning("Activation quantity PATCH failed for %s", sku)

        # Feeds API: セラーセントラルの在庫管理システムに直接反映
        # (Listings Items API PATCHだけではセラーセントラルに反映されない問題の対策)
        try:
            await sp_client.submit_price_feed(
                settings.sp_api_seller_id, sku, amazon_price,
            )
            logger.info("Price feed submitted for %s (¥%d)", sku, amazon_price)
        except AmazonApiError:
            logger.warning("Price feed failed for %s (non-critical)", sku)
        try:
            await sp_client.submit_inventory_feed(
                settings.sp_api_seller_id, sku, 1, lead_time,
            )
            logger.info("Inventory feed submitted for %s", sku)
        except AmazonApiError:
            logger.warning("Inventory feed failed for %s (non-critical)", sku)

    except AmazonApiError as e:
        raise HTTPException(502, f"SP-API error: {e}") from e

    # Update MonitoredItem with Amazon info
    item.amazon_asin = alert.amazon_asin
    item.amazon_sku = sku
    item.amazon_condition = condition
    item.amazon_listing_status = "active"
    item.amazon_price = amazon_price
    item.estimated_win_price = estimated_price
    item.shipping_cost = alert.yahoo_shipping or 0
    item.forwarding_cost = alert.forwarding_cost if alert.forwarding_cost > 0 else settings.deal_forwarding_cost
    item.amazon_fee_pct = alert.amazon_fee_pct if alert.amazon_fee_pct > 0 else settings.deal_amazon_fee_pct
    item.amazon_margin_pct = alert.gross_margin_pct if alert.gross_margin_pct > 0 else margin
    item.amazon_lead_time_days = lead_time
    item.amazon_shipping_pattern = shipping_pattern_key
    item.amazon_condition_note = body.get("condition_note", "")
    item.is_monitoring_active = True
    item.amazon_last_synced_at = datetime.now(timezone.utc)
    item.updated_at = datetime.now(timezone.utc)
    item.seller_central_checklist = ""  # チェックリストリセット

    # DealAlertを出品済みに変更（オートスキャンページから非表示）
    alert.status = "listed"

    # 履歴に記録
    db.add(StatusHistory(
        item_id=item.id, auction_id=item.auction_id,
        change_type="amazon_listing",
        new_status=sku,
    ))
    db.commit()

    return {
        "ok": True,
        "auction_id": alert.yahoo_auction_id,
        "amazon_sku": sku,
        "amazon_price": amazon_price,
        "condition": condition,
        "item_id": item.id,
    }


# ---------------------------------------------------------------------------
# Scan routes (literal path first, then parameterized)
# ---------------------------------------------------------------------------

@router.post("/scan", status_code=200)
async def trigger_scan():
    """Manually trigger a scan of all active keywords."""
    from ..main import app_state

    scanner = app_state.get("deal_scanner")
    if scanner is None:
        raise HTTPException(503, "Deal scanner is not available (Keepa API not configured?)")
    await scanner.scan_all()
    return {"ok": True, "message": "Scan completed"}


# ---------------------------------------------------------------------------
# Keyword routes with {keyword_id} path param — MUST be last
# ---------------------------------------------------------------------------

@router.put("/{keyword_id}", response_model=WatchedKeywordResponse)
def update_keyword(keyword_id: int, body: WatchedKeywordUpdate, db: Session = Depends(get_db)):
    kw = db.query(WatchedKeyword).filter(WatchedKeyword.id == keyword_id).first()
    if not kw:
        raise HTTPException(404, f"Keyword {keyword_id} not found")

    update_data = body.model_dump(exclude_unset=True)
    for key, val in update_data.items():
        setattr(kw, key, val)
    kw.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(kw)
    return _kw_to_response(kw, db)


@router.delete("/{keyword_id}", status_code=204)
def delete_keyword(keyword_id: int, db: Session = Depends(get_db)):
    kw = db.query(WatchedKeyword).filter(WatchedKeyword.id == keyword_id).first()
    if not kw:
        raise HTTPException(404, f"Keyword {keyword_id} not found")
    db.delete(kw)
    db.commit()


@router.get("/{keyword_id}/alerts", response_model=DealAlertListResponse)
def list_alerts(keyword_id: int, db: Session = Depends(get_db)):
    kw = db.query(WatchedKeyword).filter(WatchedKeyword.id == keyword_id).first()
    if not kw:
        raise HTTPException(404, f"Keyword {keyword_id} not found")
    alerts = (
        db.query(DealAlert)
        .filter(DealAlert.keyword_id == keyword_id)
        .order_by(DealAlert.notified_at.desc())
        .limit(100)
        .all()
    )
    return DealAlertListResponse(alerts=alerts, total=len(alerts))


@router.post("/{keyword_id}/scan", status_code=200)
async def trigger_keyword_scan(keyword_id: int):
    """Manually trigger a scan for a single keyword."""
    from ..main import app_state

    scanner = app_state.get("deal_scanner")
    if scanner is None:
        raise HTTPException(503, "Deal scanner is not available")
    new_deals = await scanner.scan_keyword_by_id(keyword_id)
    return {"ok": True, "new_deals": len(new_deals), "deals": new_deals}
