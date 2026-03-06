"""Deal alerts API: images, reject, delete, list, scan."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import DealAlert, MonitoredItem, StatusHistory

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/deals", tags=["deals"])


VALID_REJECTION_REASONS = (
    "wrong_product",   # 商品違い
    "accessory",       # 部品/アクセサリー
    "model_variant",   # モデル/バリアント違い
    "bad_price",       # 価格おかしい
    "never_show",      # 二度と出すな（同タイトルペアをブロック）
    "other",           # その他
)


@router.get("")
def list_deals(
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """List deal alerts with optional status filter."""
    q = db.query(DealAlert).order_by(DealAlert.notified_at.desc())
    if status:
        q = q.filter(DealAlert.status == status)
    total = q.count()
    alerts = q.offset(offset).limit(limit).all()
    return {"alerts": alerts, "total": total}


@router.get("/{alert_id}/images")
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


@router.post("/{alert_id}/reject", status_code=200)
def reject_alert(alert_id: int, body: dict = None, db: Session = Depends(get_db)):
    """Soft-delete a deal alert with rejection reason."""
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

    for attempt in range(3):
        try:
            db.commit()
            break
        except OperationalError:
            db.rollback()
            if attempt < 2:
                time.sleep(1)
                alert.status = "rejected"
                alert.rejection_reason = reason
                alert.rejection_note = body.get("note", "")
                alert.rejected_at = datetime.now(timezone.utc)
            else:
                logger.warning("DB locked during reject after 3 attempts")
                raise HTTPException(503, "データベースが一時的にビジーです。再試行してください。")

    return {"ok": True, "id": alert_id, "reason": reason}


@router.delete("/{alert_id}", status_code=204)
def delete_alert(alert_id: int, db: Session = Depends(get_db)):
    """Hard-delete a deal alert."""
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
                alert = db.query(DealAlert).filter(DealAlert.id == alert_id).first()
                if not alert:
                    return
                db.delete(alert)
            else:
                logger.warning("DB locked during delete after 3 attempts")
                raise HTTPException(503, "データベースが一時的にビジーです。再試行してください。")


@router.post("/{alert_id}/mark-listed", status_code=200)
def mark_alert_listed(alert_id: int, db: Session = Depends(get_db)):
    """Mark a deal alert as listed."""
    alert = db.query(DealAlert).filter(DealAlert.id == alert_id).first()
    if not alert:
        raise HTTPException(404, "Alert not found")
    alert.status = "listed"
    for attempt in range(3):
        try:
            db.commit()
            return {"ok": True, "id": alert_id}
        except OperationalError:
            db.rollback()
            if attempt < 2:
                time.sleep(1)
                alert.status = "listed"
            else:
                raise HTTPException(503, "データベースが一時的にビジーです。再試行してください。")


@router.post("/{alert_id}/list", status_code=201)
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
    from ..amazon.listing import ListingParams, submit_to_amazon
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
            win_price=alert.yahoo_price,
        )
        db.add(item)
        db.flush()

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

        product_type = await sp_client.get_product_type(alert.amazon_asin)
        condition_note = body.get("condition_note", "").strip()

        result = await submit_to_amazon(
            sp_client,
            ListingParams(
                seller_id=settings.sp_api_seller_id,
                sku=sku,
                asin=alert.amazon_asin,
                price=amazon_price,
                condition=condition,
                lead_time=lead_time,
                shipping_template=pattern.template_name,
                product_type=product_type,
                condition_note=condition_note,
                image_urls=body.get("image_urls", []),
                auction_id=alert.yahoo_auction_id,
            ),
        )

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
    if result.s3_image_urls:
        item.amazon_image_urls = json.dumps(result.s3_image_urls)
    elif body.get("image_urls"):
        item.amazon_image_urls = json.dumps(body["image_urls"])
    item.is_monitoring_active = True
    item.amazon_last_synced_at = datetime.now(timezone.utc)
    item.updated_at = datetime.now(timezone.utc)
    item.seller_central_checklist = ""

    # DealAlertを出品済みに変更
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


@router.post("/scan", status_code=200)
async def trigger_scan():
    """Manually trigger a deal scan."""
    from ..main import app_state

    scanner = app_state.get("deal_scanner")
    if scanner is None:
        raise HTTPException(503, "Deal scanner is not available (Keepa API not configured?)")
    await scanner.scan_all()
    return {"ok": True, "message": "Scan completed"}
