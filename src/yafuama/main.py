"""FastAPI application with lifespan-managed scraper and scheduler."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api.router import api_router
from .web.views import router as web_router
from .config import settings
from .database import run_migrations
from .monitor.scheduler import MonitorScheduler
from .notifier.log_notifier import LogNotifier
from .notifier.webhook import WebhookNotifier
from .scraper.yahoo import YahooAuctionScraper

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
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
    logger.info("ヤフアマ stopped")


app = FastAPI(
    title="ヤフアマ",
    description="ヤフオク→Amazon無在庫転売の在庫連動ツール",
    version="0.1.0",
    lifespan=lifespan,
)
if settings.api_key:
    from .auth import ApiKeyMiddleware
    app.add_middleware(ApiKeyMiddleware)

app.include_router(api_router)
app.include_router(web_router)
