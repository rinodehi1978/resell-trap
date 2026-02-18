"""Health check and scheduler control endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import MonitoredItem
from ..schemas import HealthResponse

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health", response_model=HealthResponse)
def health_check(db: Session = Depends(get_db)):
    from ..main import app_state

    total = db.query(MonitoredItem).count()
    active = db.query(MonitoredItem).filter(MonitoredItem.status == "active").count()
    scheduler = app_state.get("scheduler")
    running = scheduler.running if scheduler else False
    return HealthResponse(
        status="ok",
        scheduler_running=running,
        monitored_count=total,
        active_count=active,
    )


@router.post("/scheduler/pause")
def pause_scheduler():
    from ..main import app_state

    scheduler = app_state.get("scheduler")
    if scheduler:
        scheduler.pause()
    return {"status": "paused"}


@router.post("/scheduler/resume")
def resume_scheduler():
    from ..main import app_state

    scheduler = app_state.get("scheduler")
    if scheduler:
        scheduler.resume()
    return {"status": "resumed"}
