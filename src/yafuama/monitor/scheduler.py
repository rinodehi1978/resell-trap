"""APScheduler-based monitoring loop with smart interval adjustment."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal
from ..models import MonitoredItem, NotificationLog, StatusHistory
from ..notifier.base import BaseNotifier
from ..scraper.yahoo import YahooAuctionScraper

logger = logging.getLogger(__name__)


class MonitorScheduler:
    def __init__(
        self,
        scraper: YahooAuctionScraper,
        notifiers: list[BaseNotifier],
    ) -> None:
        self.scraper = scraper
        self.notifiers = notifiers
        self._scheduler = AsyncIOScheduler()
        self.running = False

    def start(self) -> None:
        self._scheduler.add_job(
            self._check_all,
            "interval",
            seconds=settings.min_check_interval,
            id="monitor_loop",
            replace_existing=True,
        )
        self._scheduler.start()
        self.running = True
        logger.info("Monitor scheduler started")

    def pause(self) -> None:
        self._scheduler.pause()
        self.running = False
        logger.info("Monitor scheduler paused")

    def resume(self) -> None:
        self._scheduler.resume()
        self.running = True
        logger.info("Monitor scheduler resumed")

    def add_deal_scan_job(self, scanner, interval_seconds: int) -> None:
        """Register the deal scanner as a periodic job."""
        self._scheduler.add_job(
            scanner.scan_all,
            "interval",
            seconds=interval_seconds,
            id="deal_scan",
            replace_existing=True,
        )
        logger.info("Deal scanner job registered (interval=%ds)", interval_seconds)

    def add_discovery_job(self, engine, interval_seconds: int) -> None:
        """Register the AI discovery engine as a periodic job."""
        self._scheduler.add_job(
            engine.run_discovery_cycle,
            "interval",
            seconds=interval_seconds,
            id="ai_discovery",
            replace_existing=True,
        )
        logger.info("AI Discovery job registered (interval=%ds)", interval_seconds)

    def add_listing_sync_job(self, checker, interval_seconds: int) -> None:
        """Register the Amazon listing sync checker as a periodic job."""
        self._scheduler.add_job(
            checker.check_all,
            "interval",
            seconds=interval_seconds,
            id="listing_sync",
            replace_existing=True,
        )
        logger.info("Listing sync job registered (interval=%ds)", interval_seconds)

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)
        self.running = False
        logger.info("Monitor scheduler shut down")

    async def _check_all(self) -> None:
        """Main loop: check all active items that are due."""
        db: Session = SessionLocal()
        try:
            items = (
                db.query(MonitoredItem)
                .filter(
                    MonitoredItem.is_monitoring_active == True,
                    MonitoredItem.status == "active",
                )
                .all()
            )

            now = datetime.now(timezone.utc)
            for item in items:
                interval = self._effective_interval(item)
                if item.last_checked_at and (now - item.last_checked_at).total_seconds() < interval:
                    continue
                await self._check_item(item, db)

            # Auto-cleanup ended items (3 days after ending)
            self._cleanup_ended_items(db, now)

            db.commit()
        except Exception as e:
            logger.exception("Error in monitor loop: %s", e)
            db.rollback()
        finally:
            db.close()

    async def _check_item(self, item: MonitoredItem, db: Session) -> None:
        logger.debug("Checking %s (%s)", item.auction_id, item.title)
        data = await self.scraper.fetch_auction(item.auction_id)
        if not data:
            logger.warning("Failed to fetch %s", item.auction_id)
            return

        changes: list[StatusHistory] = []

        if data.status != item.status:
            changes.append(StatusHistory(
                item_id=item.id, auction_id=item.auction_id, change_type="status_change",
                old_status=item.status, new_status=data.status,
            ))
        if data.current_price != item.current_price:
            changes.append(StatusHistory(
                item_id=item.id, auction_id=item.auction_id, change_type="price_change",
                old_price=item.current_price, new_price=data.current_price,
            ))
        if data.bid_count != item.bid_count:
            changes.append(StatusHistory(
                item_id=item.id, auction_id=item.auction_id, change_type="bid_change",
                old_bid_count=item.bid_count, new_bid_count=data.bid_count,
            ))

        # Update item
        item.title = data.title
        item.current_price = data.current_price
        item.win_price = data.win_price
        item.bid_count = data.bid_count
        item.status = data.status
        item.last_checked_at = datetime.now(timezone.utc)
        item.updated_at = datetime.now(timezone.utc)

        # Stop monitoring ended items
        if data.status != "active":
            item.is_monitoring_active = False
            logger.info("Item %s ended (%s), stopping monitor", item.auction_id, data.status)

        for change in changes:
            db.add(change)
            await self._send_notifications(item, change, db)

    async def _send_notifications(
        self, item: MonitoredItem, change: StatusHistory, db: Session
    ) -> None:
        for notifier in self.notifiers:
            channel = type(notifier).__name__
            try:
                # Amazon SKUを記録（notifier内でクリアされる前に保存）
                sku_before = item.amazon_sku
                success = await notifier.notify(item, change)
                event_type = self._event_type(change)
                db.add(NotificationLog(
                    item_id=item.id,
                    channel=channel,
                    event_type=event_type,
                    message=notifier.format_message(item, change),
                    success=success,
                ))
                # AmazonNotifierがSKUをクリアした場合、取り下げ履歴を記録
                if sku_before and not item.amazon_sku and item.amazon_listing_status == "delisted":
                    db.add(StatusHistory(
                        item_id=item.id, auction_id=item.auction_id,
                        change_type="amazon_delist_auto",
                        old_status=sku_before,
                    ))
                elif sku_before and item.amazon_listing_status == "error":
                    db.add(StatusHistory(
                        item_id=item.id, auction_id=item.auction_id,
                        change_type="amazon_error",
                        old_status=sku_before,
                        new_status="取り下げ失敗",
                    ))
            except Exception as e:
                logger.warning("Notifier %s failed: %s", channel, e)
                db.add(NotificationLog(
                    item_id=item.id,
                    channel=channel,
                    event_type="error",
                    message=str(e),
                    success=False,
                ))

    @staticmethod
    def _event_type(change: StatusHistory) -> str:
        if change.change_type == "status_change":
            if change.new_status == "ended_sold":
                return "sold"
            return "ended"
        return change.change_type

    @staticmethod
    def _effective_interval(item: MonitoredItem) -> float:
        """Smart interval: shorten as end_time approaches."""
        if not item.auto_adjust_interval or not item.end_time:
            return item.check_interval_seconds

        now = datetime.now(timezone.utc)
        # Handle timezone-aware end_time
        end = item.end_time.replace(tzinfo=None) if item.end_time.tzinfo else item.end_time
        remaining = (end - now).total_seconds()

        if remaining <= 0:
            return item.check_interval_seconds  # will be stopped after check
        if remaining < 1800:  # < 30 min
            return settings.min_check_interval
        if remaining < 7200:  # < 2 hours
            return item.check_interval_seconds / 2

        return item.check_interval_seconds

    @staticmethod
    def _cleanup_ended_items(db: Session, now: datetime) -> None:
        """Auto-delete ended MonitoredItems immediately.

        Conditions for cleanup:
        - Yahoo auction ended (status starts with 'ended_')
        - Amazon listing is NOT active (already deactivated or never listed)
        - Amazon listing is NOT in error state (needs manual attention)
        """
        stale = (
            db.query(MonitoredItem)
            .filter(
                MonitoredItem.status.like("ended_%"),
                MonitoredItem.amazon_listing_status != "active",
                MonitoredItem.amazon_listing_status != "error",
            )
            .all()
        )
        if stale:
            for item in stale:
                logger.info(
                    "Auto-cleanup: removing ended item %s (%s)",
                    item.auction_id, item.status,
                )
                db.delete(item)
            logger.info("Auto-cleanup: removed %d ended items", len(stale))
