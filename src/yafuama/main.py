"""FastAPI application with lifespan-managed scraper and scheduler."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .api.router import api_router
from .web.views import router as web_router
from .config import settings
from .database import run_migrations
from .monitor.scheduler import MonitorScheduler
from .notifier.log_notifier import LogNotifier
from .notifier.webhook import WebhookNotifier
from .scraper.yahoo import YahooAuctionScraper

_log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
_log_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=_log_level, format=_log_fmt)
# Rotating file handler: max 5MB × 3 files
# Use the same directory as the database (writable in Docker /data/)
import os as _os
_log_dir = _os.path.dirname(settings.database_url.replace("sqlite:///", "")) or "."
_log_path = _os.path.join(_log_dir, "yafuama.log")
try:
    _file_handler = RotatingFileHandler(_log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter(_log_fmt))
    _file_handler.setLevel(_log_level)
    logging.getLogger().addHandler(_file_handler)
except PermissionError:
    pass  # Docker: /app is read-only, skip file logging
logger = logging.getLogger(__name__)

# Shared state accessible by API endpoints
app_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Running database migrations...")
    run_migrations()

    scraper = YahooAuctionScraper()
    app_state["scraper"] = scraper

    notifiers = [LogNotifier()]
    if settings.webhook_url:
        notifiers.append(WebhookNotifier())

    # Amazon SP-API (graceful degradation)
    if settings.sp_api_enabled:
        from .amazon.client import SpApiClient
        from .amazon.notifier import AmazonNotifier

        sp_client = SpApiClient()
        app_state["sp_api"] = sp_client
        notifiers.append(AmazonNotifier(sp_client, settings.sp_api_seller_id))
        logger.info("Amazon SP-API integration enabled")
    else:
        logger.info("Amazon SP-API not configured — skipping")

    # Keepa API (graceful degradation)
    if settings.keepa_enabled:
        from .keepa.client import KeepaClient

        keepa_client = KeepaClient()
        app_state["keepa"] = keepa_client
        logger.info("Keepa API integration enabled")
    else:
        keepa_client = None
        logger.info("Keepa API not configured — skipping")

    scheduler = MonitorScheduler(scraper, notifiers)
    scheduler.start()
    app_state["scheduler"] = scheduler

    # Deal scanner (requires both scraper and Keepa)
    if keepa_client is not None:
        from .monitor.deal_scanner import DealScanner

        deal_scanner = DealScanner(
            scraper, keepa_client,
            webhook_url=settings.webhook_url,
            webhook_type=settings.webhook_type,
            sp_api_client=app_state.get("sp_api"),
        )
        app_state["deal_scanner"] = deal_scanner
        scheduler.add_deal_scan_job(deal_scanner, settings.deal_scan_interval)
        logger.info("Deal scanner enabled (interval=%ds)", settings.deal_scan_interval)

        # AI Discovery engine (requires Keepa + deal_scanner)
        if settings.discovery_enabled:
            from .ai.engine import DiscoveryEngine

            discovery_engine = DiscoveryEngine(
                scraper, keepa_client,
                anthropic_api_key=settings.anthropic_api_key,
            )
            app_state["discovery_engine"] = discovery_engine
            scheduler.add_discovery_job(discovery_engine, settings.discovery_interval)
            logger.info("AI Discovery engine enabled (interval=%ds)", settings.discovery_interval)

    # Amazon listing sync (detect deletions from Seller Central)
    if "sp_api" in app_state:
        from .amazon.listing_sync import ListingSyncChecker

        listing_checker = ListingSyncChecker(app_state["sp_api"])
        scheduler.add_listing_sync_job(listing_checker, 3600)  # 1時間ごと
        logger.info("Amazon listing sync enabled (interval=3600s)")

    # Amazon order monitor (注文通知)
    if "sp_api" in app_state and settings.order_monitor_enabled and (settings.order_webhook_url or settings.webhook_url):
        from .amazon.order_monitor import OrderMonitor

        order_monitor = OrderMonitor(
            client=app_state["sp_api"],
            webhook_url=settings.order_webhook_url or settings.webhook_url,
            webhook_type=settings.webhook_type,
        )
        app_state["order_monitor"] = order_monitor
        scheduler.add_order_monitor_job(order_monitor, settings.order_monitor_interval)
        logger.info(
            "Amazon order monitor enabled (interval=%ds)",
            settings.order_monitor_interval,
        )

    # Load matcher overrides from rejection patterns
    try:
        from .matcher_overrides import overrides as matcher_overrides
        matcher_overrides.reload()
    except Exception as e:
        logger.warning("Failed to load matcher overrides: %s", e)

    logger.info("ヤフアマ started")

    # Notify startup (detects restarts after crashes)
    try:
        from .notifier.health import notify_startup
        await notify_startup()
    except Exception as e:
        logger.warning("Startup notification failed: %s", e)

    yield

    # Shutdown
    scheduler.shutdown()
    await scraper.close()
    if "keepa" in app_state:
        await app_state["keepa"].close()
    app_state.clear()

    # SQLite WAL cleanup (shrink .db-wal file)
    if settings.database_url.startswith("sqlite"):
        try:
            import sqlalchemy as sa
            from .database import engine
            with engine.connect() as conn:
                conn.execute(sa.text("PRAGMA wal_checkpoint(TRUNCATE)"))
            logger.info("SQLite WAL checkpoint completed")
        except Exception:
            pass

    logger.info("ヤフアマ stopped")


app = FastAPI(
    title="ヤフアマ",
    description="ヤフオク→Amazon無在庫転売の在庫連動ツール",
    version="0.1.0",
    lifespan=lifespan,
)


class NoCacheMiddleware(BaseHTTPMiddleware):
    """Prevent mobile browsers from serving stale HTML."""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        ct = response.headers.get("content-type", "")
        if "text/html" in ct:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


app.add_middleware(NoCacheMiddleware)
if settings.api_key:
    from .auth import ApiKeyMiddleware
    app.add_middleware(ApiKeyMiddleware)

app.include_router(api_router)
app.include_router(web_router)
