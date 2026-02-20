"""History and notification log endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import MonitoredItem, NotificationLog, StatusHistory
from ..schemas import NotificationLogResponse, StatusHistoryResponse

router = APIRouter(prefix="/api/items", tags=["status"])


@router.get("/{auction_id}/history", response_model=list[StatusHistoryResponse])
def get_history(auction_id: str, db: Session = Depends(get_db)):
    item = db.query(MonitoredItem).filter(MonitoredItem.auction_id == auction_id).first()
    if not item:
        raise HTTPException(404, f"Item {auction_id} not found")
    records = (
        db.query(StatusHistory)
        .filter(StatusHistory.item_id == item.id)
        .order_by(StatusHistory.recorded_at.desc())
        .all()
    )
    return records


@router.get("/{auction_id}/notifications", response_model=list[NotificationLogResponse])
def get_notifications(auction_id: str, db: Session = Depends(get_db)):
    item = db.query(MonitoredItem).filter(MonitoredItem.auction_id == auction_id).first()
    if not item:
        raise HTTPException(404, f"Item {auction_id} not found")
    logs = (
        db.query(NotificationLog)
        .filter(NotificationLog.item_id == item.id)
        .order_by(NotificationLog.sent_at.desc())
        .all()
    )
    return logs
