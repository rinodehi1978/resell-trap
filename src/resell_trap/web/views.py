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


@router.get("/deals", response_class=HTMLResponse)
def deals_page(request: Request):
    return templates.TemplateResponse("deals.html", {
        "request": request,
        "active_page": "deals",
        "query": "",
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


@router.get("/partials/deals-result", response_class=HTMLResponse)
async def deals_result_partial(
    request: Request,
    q: str = Query("", min_length=1),
    shipping_cost: int = 800,
):
    """Search Yahoo Auctions, match to Amazon via Keepa, calculate profit."""
    from ..config import settings
    from ..keepa import KeepaApiError
    from ..keepa.analyzer import score_deal
    from ..main import app_state

    scraper = app_state.get("scraper")
    keepa = app_state.get("keepa")

    if not scraper:
        return templates.TemplateResponse("partials/deals_result.html", {
            "request": request, "error": "Scraper not available.", "deals": [], "tokens_left": None,
        })
    if not keepa:
        return templates.TemplateResponse("partials/deals_result.html", {
            "request": request, "error": "Keepa API is not configured.", "deals": [], "tokens_left": None,
        })

    # Step 1: Search Yahoo Auctions
    try:
        yahoo_results = await scraper.search(q, page=1)
    except Exception as e:
        return templates.TemplateResponse("partials/deals_result.html", {
            "request": request, "error": f"Yahoo search error: {e}", "deals": [], "tokens_left": None,
        })

    if not yahoo_results:
        return templates.TemplateResponse("partials/deals_result.html", {
            "request": request, "error": None, "deals": [], "tokens_left": keepa.tokens_left,
        })

    # Step 2: Search Keepa for matching Amazon products
    try:
        keepa_products = await keepa.search_products(q, stats=settings.keepa_default_stats_days)
    except KeepaApiError as e:
        return templates.TemplateResponse("partials/deals_result.html", {
            "request": request, "error": f"Keepa search error: {e}", "deals": [], "tokens_left": keepa.tokens_left,
        })

    if not keepa_products:
        return templates.TemplateResponse("partials/deals_result.html", {
            "request": request, "error": None, "deals": [], "tokens_left": keepa.tokens_left,
        })

    # Step 3: For each Yahoo result, find best matching Amazon product and score
    deals = []
    for yr in yahoo_results:
        yahoo_price = yr.current_price if hasattr(yr, "current_price") else yr.get("current_price", 0)
        if yahoo_price <= 0:
            continue

        yahoo_title = yr.title if hasattr(yr, "title") else yr.get("title", "")
        yahoo_title_lower = yahoo_title.lower()

        # Find best Keepa match by title similarity
        best_deal = None
        best_score = -1

        for kp in keepa_products:
            amazon_title = (kp.get("title") or "").lower()
            if not amazon_title:
                continue

            # Simple word overlap scoring
            yahoo_words = set(yahoo_title_lower.split())
            amazon_words = set(amazon_title.split())
            overlap = len(yahoo_words & amazon_words)
            if overlap < 2:
                continue

            deal = score_deal(
                yahoo_price=yahoo_price,
                keepa_product=kp,
                shipping_cost=shipping_cost,
                margin_pct=settings.sp_api_default_margin_pct,
                good_rank_threshold=settings.keepa_good_rank_threshold,
            )
            if deal and overlap > best_score:
                best_score = overlap
                deal.yahoo_title = yahoo_title
                deal.yahoo_price = yahoo_price
                deal.yahoo_auction_id = yr.auction_id if hasattr(yr, "auction_id") else yr.get("auction_id", "")
                deal.yahoo_url = yr.url if hasattr(yr, "url") else yr.get("url", "")
                deal.yahoo_image_url = yr.image_url if hasattr(yr, "image_url") else yr.get("image_url", "")
                best_deal = deal

        if best_deal:
            deals.append(best_deal)

    # Sort by profit descending
    deals.sort(key=lambda d: d.estimated_profit, reverse=True)

    return templates.TemplateResponse("partials/deals_result.html", {
        "request": request,
        "error": None,
        "deals": deals,
        "tokens_left": keepa.tokens_left,
    })


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
