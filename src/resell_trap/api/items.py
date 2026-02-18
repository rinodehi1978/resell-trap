"""CRUD endpoints for monitored items."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import MonitoredItem, StatusHistory
from ..schemas import AuctionData, ItemCreate, ItemListResponse, ItemResponse, ItemUpdate
from ..scraper.yahoo import YahooAuctionScraper, extract_auction_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/items", tags=["items"])


def _get_scraper() -> YahooAuctionScraper:
    from ..main import app_state
    return app_state["scraper"]


def _apply_auction_data(item: MonitoredItem, data: AuctionData) -> None:
    """Update a MonitoredItem from scraped auction data."""
    item.title = data.title
    item.url = data.url
    item.image_url = data.image_url
    item.category_id = data.category_id
    item.seller_id = data.seller_id or item.seller_id
    item.current_price = data.current_price
    item.start_price = data.start_price
    item.buy_now_price = data.buy_now_price
    item.win_price = data.win_price
    item.start_time = data.start_time
    item.end_time = data.end_time
    item.bid_count = data.bid_count
    item.status = data.status
    item.last_checked_at = datetime.now(timezone.utc)
    item.updated_at = datetime.now(timezone.utc)


@router.post("", response_model=ItemResponse, status_code=201)
async def create_item(body: ItemCreate, db: Session = Depends(get_db)):
    # Resolve auction_id
    raw = body.auction_id or body.url or ""
    auction_id = extract_auction_id(raw)
    if not auction_id:
        raise HTTPException(400, "auction_id or valid Yahoo Auction URL required")

    # Check duplicate
    existing = db.query(MonitoredItem).filter(MonitoredItem.auction_id == auction_id).first()
    if existing:
        raise HTTPException(409, f"Item {auction_id} already monitored")

    # Fetch auction data
    scraper = _get_scraper()
    data = await scraper.fetch_auction(auction_id)
    if not data:
        raise HTTPException(502, f"Could not fetch auction {auction_id}")

    item = MonitoredItem(
        auction_id=auction_id,
        check_interval_seconds=body.check_interval_seconds,
        auto_adjust_interval=body.auto_adjust_interval,
        notes=body.notes,
    )
    _apply_auction_data(item, data)
    db.add(item)
    db.flush()

    # Record initial history
    history = StatusHistory(
        item_id=item.id,
        auction_id=auction_id,
        change_type="initial",
        new_status=item.status,
        new_price=item.current_price,
        new_bid_count=item.bid_count,
    )
    db.add(history)
    db.commit()
    db.refresh(item)
    return item


@router.get("", response_model=ItemListResponse)
def list_items(
    status: str | None = None,
    monitoring: bool | None = None,
    db: Session = Depends(get_db),
):
    q = db.query(MonitoredItem)
    if status:
        q = q.filter(MonitoredItem.status == status)
    if monitoring is not None:
        q = q.filter(MonitoredItem.is_monitoring_active == monitoring)
    items = q.order_by(MonitoredItem.created_at.desc()).all()
    return ItemListResponse(items=items, total=len(items))


@router.get("/{auction_id}", response_model=ItemResponse)
def get_item(auction_id: str, db: Session = Depends(get_db)):
    item = db.query(MonitoredItem).filter(MonitoredItem.auction_id == auction_id).first()
    if not item:
        raise HTTPException(404, f"Item {auction_id} not found")
    return item


@router.put("/{auction_id}", response_model=ItemResponse)
def update_item(auction_id: str, body: ItemUpdate, db: Session = Depends(get_db)):
    item = db.query(MonitoredItem).filter(MonitoredItem.auction_id == auction_id).first()
    if not item:
        raise HTTPException(404, f"Item {auction_id} not found")

    update_data = body.model_dump(exclude_unset=True)
    for key, val in update_data.items():
        setattr(item, key, val)
    item.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/{auction_id}", status_code=204)
def delete_item(auction_id: str, db: Session = Depends(get_db)):
    item = db.query(MonitoredItem).filter(MonitoredItem.auction_id == auction_id).first()
    if not item:
        raise HTTPException(404, f"Item {auction_id} not found")
    db.delete(item)
    db.commit()


@router.post("/{auction_id}/refresh", response_model=ItemResponse)
async def refresh_item(auction_id: str, db: Session = Depends(get_db)):
    item = db.query(MonitoredItem).filter(MonitoredItem.auction_id == auction_id).first()
    if not item:
        raise HTTPException(404, f"Item {auction_id} not found")

    scraper = _get_scraper()
    data = await scraper.fetch_auction(auction_id)
    if not data:
        raise HTTPException(502, f"Could not fetch auction {auction_id}")

    # Detect changes
    changes: list[StatusHistory] = []
    if data.status != item.status:
        changes.append(StatusHistory(
            item_id=item.id, auction_id=auction_id, change_type="status_change",
            old_status=item.status, new_status=data.status,
        ))
    if data.current_price != item.current_price:
        changes.append(StatusHistory(
            item_id=item.id, auction_id=auction_id, change_type="price_change",
            old_price=item.current_price, new_price=data.current_price,
        ))
    if data.bid_count != item.bid_count:
        changes.append(StatusHistory(
            item_id=item.id, auction_id=auction_id, change_type="bid_change",
            old_bid_count=item.bid_count, new_bid_count=data.bid_count,
        ))

    _apply_auction_data(item, data)
    for h in changes:
        db.add(h)
    db.commit()
    db.refresh(item)
    return item
