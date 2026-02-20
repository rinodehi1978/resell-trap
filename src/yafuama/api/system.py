"""Health check and scheduler control endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import MonitoredItem
from ..schemas import HealthResponse, ServiceStatus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health", response_model=HealthResponse)
def health_check(db: Session = Depends(get_db)):
    from ..main import app_state

    services: list[ServiceStatus] = []
    overall = "ok"

    # Database
    try:
        total = db.query(MonitoredItem).count()
        active = db.query(MonitoredItem).filter(MonitoredItem.status == "active").count()
        services.append(ServiceStatus(name="database", status="ok"))
    except Exception as e:
        logger.warning("Health check: DB error: %s", e)
        total = active = 0
        services.append(ServiceStatus(name="database", status="degraded", detail=str(e)))
        overall = "degraded"

    # Scheduler
    scheduler = app_state.get("scheduler")
    running = scheduler.running if scheduler else False
    services.append(ServiceStatus(
        name="scheduler",
        status="ok" if running else "unavailable",
        detail="" if running else "not running",
    ))
    if not running:
        overall = "degraded"

    # Amazon SP-API
    sp_api = app_state.get("sp_api")
    if sp_api:
        services.append(ServiceStatus(name="amazon_sp_api", status="ok"))
    else:
        services.append(ServiceStatus(name="amazon_sp_api", status="unavailable", detail="not configured"))

    # Keepa
    keepa = app_state.get("keepa")
    if keepa:
        tokens = getattr(keepa, "tokens_left", None)
        detail = f"{tokens} tokens remaining" if tokens is not None else ""
        services.append(ServiceStatus(name="keepa", status="ok", detail=detail))
    else:
        services.append(ServiceStatus(name="keepa", status="unavailable", detail="not configured"))

    # Deal Scanner
    scanner = app_state.get("deal_scanner")
    if scanner:
        services.append(ServiceStatus(name="deal_scanner", status="ok"))
    else:
        services.append(ServiceStatus(name="deal_scanner", status="unavailable", detail="requires Keepa"))

    # AI Discovery
    discovery = app_state.get("discovery_engine")
    if discovery:
        services.append(ServiceStatus(name="ai_discovery", status="ok"))
    else:
        services.append(ServiceStatus(name="ai_discovery", status="unavailable", detail="requires Keepa"))

    return HealthResponse(
        status=overall,
        scheduler_running=running,
        monitored_count=total,
        active_count=active,
        services=services,
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
