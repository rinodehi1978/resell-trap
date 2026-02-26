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
from ..matcher import is_apparel, match_products
from ..models import DealAlert, MonitoredItem, NotificationLog, StatusHistory, WatchedKeyword

logger = logging.getLogger(__name__)

_template_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))
templates.env.auto_reload = True


def _checklist_complete(item: MonitoredItem) -> bool | None:
    """Return True if all checklist items checked, False if incomplete, None if N/A."""
    import json as _json

    if item.amazon_listing_status != "active":
        return None
    try:
        cl = _json.loads(item.seller_central_checklist) if item.seller_central_checklist else {}
    except (_json.JSONDecodeError, TypeError):
        cl = {}
    if not cl:
        return False
    return all(cl.get(k, False) for k in ("lead_time", "images", "condition"))


templates.env.globals["checklist_complete"] = _checklist_complete

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
        "amazon_listed": sum(1 for i in items if i.amazon_listing_status == "active"),
    }
    scheduler = app_state.get("scheduler")

    # Deal scanner stats
    scanner = app_state.get("deal_scanner")
    keepa = app_state.get("keepa")
    active_keywords = db.query(WatchedKeyword).filter(WatchedKeyword.is_active == True).count()  # noqa: E712
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    deals_today = db.query(DealAlert).filter(DealAlert.notified_at >= today_start).count()
    last_scan = (
        db.query(WatchedKeyword.last_scanned_at)
        .filter(WatchedKeyword.last_scanned_at.isnot(None))
        .order_by(WatchedKeyword.last_scanned_at.desc())
        .first()
    )
    scanner_stats = {
        "enabled": scanner is not None,
        "active_keywords": active_keywords,
        "deals_today": deals_today,
        "last_scan": last_scan[0] if last_scan else None,
        "keepa_tokens": getattr(keepa, "tokens_left", None) if keepa else None,
    }

    recent_deals = (
        db.query(DealAlert)
        .filter(DealAlert.status == "active")
        .order_by(DealAlert.notified_at.desc())
        .limit(5)
        .all()
    )

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "active_page": "dashboard",
        "stats": stats,
        "recent_items": items[:10],
        "scheduler_running": scheduler is not None and scheduler.running,
        "scanner_stats": scanner_stats,
        "recent_deals": recent_deals,
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
    import json as _json

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

    # Parse checklist JSON
    try:
        checklist = _json.loads(item.seller_central_checklist) if item.seller_central_checklist else {}
    except (_json.JSONDecodeError, TypeError):
        checklist = {}
    # Ensure all keys present
    for k in ("lead_time", "images", "condition"):
        checklist.setdefault(k, False)

    return templates.TemplateResponse("item_detail.html", {
        "request": request,
        "active_page": "items",
        "item": item,
        "history": history,
        "notifications": notifications,
        "checklist_json": _json.dumps(checklist),
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


@router.get("/keywords", response_class=HTMLResponse)
def keywords_page(request: Request, db: Session = Depends(get_db)):
    from ..main import app_state

    keywords = db.query(WatchedKeyword).order_by(WatchedKeyword.created_at.desc()).all()
    # Attach alert counts
    for kw in keywords:
        kw.alert_count = db.query(DealAlert).filter(DealAlert.keyword_id == kw.id).count()
    recent_alerts = (
        db.query(DealAlert)
        .filter(DealAlert.status.in_(["active", "listed"]))
        .order_by(DealAlert.notified_at.desc())
        .limit(20)
        .all()
    )
    # Mark alerts that are already monitored
    monitored_auction_ids = {
        row[0] for row in db.query(MonitoredItem.auction_id)
        .filter(MonitoredItem.amazon_sku.isnot(None))
        .all()
    }
    for alert in recent_alerts:
        alert.is_listed = alert.yahoo_auction_id in monitored_auction_ids
    scanner = app_state.get("deal_scanner")
    discovery = app_state.get("discovery_engine")

    # AI Discovery stats
    from ..models import KeywordCandidate, DiscoveryLog
    ai_keywords_active = (
        db.query(WatchedKeyword)
        .filter(WatchedKeyword.source != "manual", WatchedKeyword.is_active == True)  # noqa: E712
        .count()
    )
    auto_added_count = (
        db.query(KeywordCandidate)
        .filter(KeywordCandidate.status == "auto_added")
        .count()
    )
    ai_deals = (
        db.query(DealAlert)
        .join(WatchedKeyword)
        .filter(WatchedKeyword.source != "manual")
        .count()
    )
    last_log = db.query(DiscoveryLog).order_by(DiscoveryLog.id.desc()).first()
    from ..config import settings as app_settings

    # Monitored items for bottom section
    monitored_items = (
        db.query(MonitoredItem)
        .filter(MonitoredItem.is_monitoring_active == True)  # noqa: E712
        .order_by(MonitoredItem.created_at.desc())
        .all()
    )

    return templates.TemplateResponse("keywords.html", {
        "request": request,
        "active_page": "keywords",
        "keywords": keywords,
        "recent_alerts": recent_alerts,
        "scanner_available": scanner is not None,
        "discovery_available": discovery is not None,
        "ai_keywords_active": ai_keywords_active,
        "auto_added_count": auto_added_count,
        "ai_deals": ai_deals,
        "last_discovery_log": last_log,
        "anthropic_configured": bool(app_settings.anthropic_api_key),
        "monitored_items": monitored_items,
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
    forwarding_cost: int = 0,
):
    """Search Yahoo Auctions, match to Amazon via Keepa, calculate profit.

    Uses gross margin model:
      total_cost = yahoo_price + yahoo_shipping + forwarding + system_fee
      gross_profit = amazon_sell_price - total_cost - amazon_fee
      gross_margin = gross_profit / amazon_sell_price * 100
    Filters: gross_margin >= deal_min_gross_margin_pct AND gross_profit >= deal_min_gross_profit
    """
    from ..config import settings
    from ..keepa import KeepaApiError
    from ..keepa.analyzer import score_deal
    from ..main import app_state

    scraper = app_state.get("scraper")
    keepa = app_state.get("keepa")

    # Use config defaults if UI sent 0 (meaning "use default")
    fwd_cost = forwarding_cost if forwarding_cost > 0 else settings.deal_forwarding_cost

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
        # Only consider items with a buy-it-now price (即決価格).
        # Auction-only items (no buy-now) have unpredictable final prices.
        buy_now = yr.buy_now_price if hasattr(yr, "buy_now_price") else yr.get("buy_now_price", 0)
        if buy_now <= 0:
            continue
        yahoo_price = buy_now

        yahoo_title = yr.title if hasattr(yr, "title") else yr.get("title", "")

        # Skip apparel / fashion items
        if is_apparel(yahoo_title):
            continue

        # Yahoo listing shipping (scraped, may be None = unknown)
        yr_shipping = yr.shipping_cost if hasattr(yr, "shipping_cost") else yr.get("shipping_cost")
        yahoo_shipping = yr_shipping if yr_shipping is not None else settings.deal_default_shipping

        # Find best Keepa match by title similarity
        best_deal = None
        best_score = -1

        for kp in keepa_products:
            amazon_title = kp.get("title") or ""
            if not amazon_title:
                continue

            result = match_products(yahoo_title, amazon_title)
            if not result.is_likely_match:
                continue

            deal = score_deal(
                yahoo_price=yahoo_price,
                keepa_product=kp,
                yahoo_shipping=yahoo_shipping,
                forwarding_cost=fwd_cost,
                amazon_fee_pct=settings.deal_amazon_fee_pct,
                good_rank_threshold=settings.keepa_good_rank_threshold,
            )
            if deal and result.score > best_score:
                # Price ratio sanity check: if Yahoo < 25% of Amazon,
                # it's likely an accessory/part, not the real product
                if deal.sell_price > 0 and yahoo_price < deal.sell_price * 0.25:
                    continue
                # Strict check for high-margin deals (50%+)
                if deal.gross_margin_pct >= settings.deal_strict_margin_pct:
                    if not result.passes_strict_check():
                        continue
                best_score = result.score
                deal.yahoo_title = yahoo_title
                deal.yahoo_price = yahoo_price
                deal.yahoo_shipping = yahoo_shipping
                deal.yahoo_auction_id = yr.auction_id if hasattr(yr, "auction_id") else yr.get("auction_id", "")
                deal.yahoo_url = yr.url if hasattr(yr, "url") else yr.get("url", "")
                deal.yahoo_image_url = yr.image_url if hasattr(yr, "image_url") else yr.get("image_url", "")
                best_deal = deal

        if best_deal:
            deals.append(best_deal)

    # Filter: gross margin >= min AND <= max AND gross profit >= minimum
    min_margin = settings.deal_min_gross_margin_pct
    max_margin = settings.deal_max_gross_margin_pct
    min_profit = settings.deal_min_gross_profit
    filtered = [d for d in deals
                if d.gross_margin_pct >= min_margin
                and d.gross_margin_pct <= max_margin
                and d.gross_profit >= min_profit]

    # Sort by gross profit descending
    filtered.sort(key=lambda d: d.gross_profit, reverse=True)

    return templates.TemplateResponse("partials/deals_result.html", {
        "request": request,
        "error": None,
        "deals": filtered,
        "tokens_left": keepa.tokens_left,
        "total_before_filter": len(deals),
        "min_margin": min_margin,
        "min_profit": min_profit,
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
