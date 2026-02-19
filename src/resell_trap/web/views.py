"""Web UI views — serves Jinja2 templates for the browser dashboard."""

from __future__ import annotations

import logging
from pathlib import Path

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import MonitoredItem, NotificationLog, StatusHistory

logger = logging.getLogger(__name__)

_template_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))

router = APIRouter(tags=["web"])


# --- Full pages ---


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    from ..main import app_state

    items = db.query(MonitoredItem).order_by(MonitoredItem.created_at.desc()).all()
    stats = {
        "total": len(items),
        "active": sum(1 for i in items if i.status == "active"),
        "sold": sum(1 for i in items if i.status == "ended_sold"),
        "amazon_listed": sum(1 for i in items if i.amazon_sku),
    }
    scheduler = app_state.get("scheduler")
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "active_page": "dashboard",
        "stats": stats,
        "recent_items": items[:10],
        "scheduler_running": scheduler is not None and scheduler.running,
    })


@router.get("/items", response_class=HTMLResponse)
def items_page(
    request: Request,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    q = db.query(MonitoredItem)
    if status:
        q = q.filter(MonitoredItem.status == status)
    items = q.order_by(MonitoredItem.created_at.desc()).all()
    return templates.TemplateResponse("items.html", {
        "request": request,
        "active_page": "items",
        "items": items,
        "status_filter": status or "",
    })


@router.get("/items/{auction_id}", response_class=HTMLResponse)
def item_detail(request: Request, auction_id: str, db: Session = Depends(get_db)):
    item = db.query(MonitoredItem).filter(MonitoredItem.auction_id == auction_id).first()
    if not item:
        return HTMLResponse("<h1>Not Found</h1>", status_code=404)
    history = (
        db.query(StatusHistory)
        .filter(StatusHistory.item_id == item.id)
        .order_by(StatusHistory.recorded_at.desc())
        .limit(50)
        .all()
    )
    notifications = (
        db.query(NotificationLog)
        .filter(NotificationLog.item_id == item.id)
        .order_by(NotificationLog.sent_at.desc())
        .limit(50)
        .all()
    )
    return templates.TemplateResponse("item_detail.html", {
        "request": request,
        "active_page": "items",
        "item": item,
        "history": history,
        "notifications": notifications,
    })


@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request):
    return templates.TemplateResponse("search.html", {
        "request": request,
        "active_page": "search",
        "query": "",
        "results": None,
    })


@router.get("/keepa", response_class=HTMLResponse)
def keepa_page(request: Request):
    return templates.TemplateResponse("keepa.html", {
        "request": request,
        "active_page": "keepa",
        "asin": "",
        "cost_price": "",
        "shipping_cost": "800",
        "analysis": None,
    })


# --- htmx partials ---


@router.get("/partials/health", response_class=HTMLResponse)
def health_partial(request: Request, db: Session = Depends(get_db)):
    from ..main import app_state

    scheduler = app_state.get("scheduler")
    total = db.query(MonitoredItem).count()
    active = db.query(MonitoredItem).filter(MonitoredItem.is_monitoring_active == True).count()  # noqa: E712
    health = {
        "scheduler_running": scheduler is not None and scheduler.running,
        "monitored_count": total,
        "active_count": active,
    }
    return templates.TemplateResponse("partials/health.html", {
        "request": request,
        "health": health,
    })


@router.get("/partials/search-results", response_class=HTMLResponse)
async def search_results_partial(
    request: Request,
    q: str = Query("", min_length=1),
):
    from ..main import app_state

    scraper = app_state["scraper"]
    results = await scraper.search(q, page=1)
    return templates.TemplateResponse("partials/search_results.html", {
        "request": request,
        "results": results,
    })


@router.get("/partials/keepa-result", response_class=HTMLResponse)
async def keepa_result_partial(
    request: Request,
    asin: str = "",
    cost_price: int = 0,
    shipping_cost: int = 800,
):
    if not asin:
        return HTMLResponse("<p class='text-sm text-gray-500'>Enter an ASIN to analyze.</p>")

    from ..config import settings
    from ..keepa import KeepaApiError
    from ..keepa.analyzer import analyze_product
    from ..main import app_state

    client = app_state.get("keepa")
    if client is None:
        return templates.TemplateResponse("partials/keepa_result.html", {
            "request": request,
            "error": "Keepa API is not configured.",
            "analysis": None,
        })

    try:
        product = await client.query_product(asin, stats=settings.keepa_default_stats_days)
    except KeepaApiError as e:
        return templates.TemplateResponse("partials/keepa_result.html", {
            "request": request,
            "error": f"Keepa API error: {e}",
            "analysis": None,
        })

    result = analyze_product(
        product,
        cost_price=cost_price,
        shipping_cost=shipping_cost,
        margin_pct=settings.sp_api_default_margin_pct,
        good_rank_threshold=settings.keepa_good_rank_threshold,
    )

    # Convert dataclasses to dicts for template
    analysis = {
        "asin": result.asin,
        "title": result.title,
        "sales_rank": result.sales_rank.__dict__,
        "used_price": result.used_price.__dict__,
        "recommendation": result.recommendation.__dict__ if result.recommendation else None,
    }

    return templates.TemplateResponse("partials/keepa_result.html", {
        "request": request,
        "error": None,
        "analysis": _to_ns(analysis),
    })


@router.get("/partials/keepa-inline/{asin}", response_class=HTMLResponse)
async def keepa_inline_partial(
    request: Request,
    asin: str,
    cost_price: int = 0,
    shipping_cost: int = 800,
):
    return await keepa_result_partial(request, asin, cost_price, shipping_cost)


@router.post("/web/set-asin/{auction_id}")
def set_asin(
    auction_id: str,
    data: dict = Body(...),
    db: Session = Depends(get_db),
):
    """Set ASIN and cost parameters on an item (called from item detail UI)."""
    item = db.query(MonitoredItem).filter(MonitoredItem.auction_id == auction_id).first()
    if not item:
        return JSONResponse({"detail": "Item not found"}, status_code=404)

    asin = data.get("asin", "").strip()
    if not asin:
        return JSONResponse({"detail": "ASIN is required"}, status_code=400)

    item.amazon_asin = asin
    if "estimated_win_price" in data:
        item.estimated_win_price = int(data["estimated_win_price"])
    if "shipping_cost" in data:
        item.shipping_cost = int(data["shipping_cost"])
    item.updated_at = datetime.now(timezone.utc)
    db.commit()
    return JSONResponse({"ok": True})


class _Namespace:
    """Simple dot-access wrapper for dicts, used in templates."""

    def __init__(self, data: dict):
        for k, v in data.items():
            if isinstance(v, dict):
                setattr(self, k, _Namespace(v))
            else:
                setattr(self, k, v)


def _to_ns(data):
    if isinstance(data, dict):
        return _Namespace(data)
    return data
