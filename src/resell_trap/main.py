"""FastAPI application with lifespan-managed scraper and scheduler."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api.router import api_router
from .config import settings
from .database import init_db
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
    logger.info("Initializing database...")
    init_db()

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
        logger.info("Keepa API not configured — skipping")

    scheduler = MonitorScheduler(scraper, notifiers)
    scheduler.start()
    app_state["scheduler"] = scheduler

    logger.info("Resell Trap started")
    yield

    # Shutdown
    scheduler.shutdown()
    await scraper.close()
    if "keepa" in app_state:
        await app_state["keepa"].close()
    app_state.clear()
    logger.info("Resell Trap stopped")


app = FastAPI(
    title="Resell Trap",
    description="ヤフオク→Amazon無在庫転売の在庫連動ツール",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(api_router)
