"""APScheduler-based monitoring loop with smart interval adjustment."""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal
from ..models import (
    AmazonOrder, DealAlert, DiscoveryLog, KeywordCandidate, MonitoredItem,
    NotificationLog, StatusHistory,
)
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
            seconds=60,  # 60秒tickで十分（実際のスクレイピング間隔はcheck_interval_seconds=300）
            id="monitor_loop",
            replace_existing=True,
            max_instances=1,
        )
        self._scheduler.add_job(
            self._retry_failed_amazon_deletions,
            "interval",
            seconds=600,  # 10分ごと
            id="amazon_delete_retry",
            replace_existing=True,
            max_instances=1,
        )
        if settings.relist_check_enabled:
            self._scheduler.add_job(
                self._check_relist_candidates,
                "interval",
                seconds=settings.relist_check_interval,
                id="relist_check",
                replace_existing=True,
                max_instances=1,
            )
            logger.info(
                "Relist check job registered (interval=%ds, max_days=%d)",
                settings.relist_check_interval, settings.relist_check_max_days,
            )
        self._scheduler.add_job(
            self._data_retention_cleanup,
            "interval",
            seconds=86400,  # 24時間ごと
            id="data_retention",
            replace_existing=True,
            max_instances=1,
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
            max_instances=1,
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
            max_instances=1,
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
            max_instances=1,
        )
        logger.info("Listing sync job registered (interval=%ds)", interval_seconds)

    def add_order_monitor_job(self, monitor, interval_seconds: int) -> None:
        """Register the Amazon order monitor as a periodic job."""
        self._scheduler.add_job(
            monitor.check_orders,
            "interval",
            seconds=interval_seconds,
            id="order_monitor",
            replace_existing=True,
            max_instances=1,
        )
        logger.info("Order monitor job registered (interval=%ds)", interval_seconds)

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)
        self.running = False
        logger.info("Monitor scheduler shut down")

    async def _check_all(self) -> None:
        """Main loop: check all active items that are due.

        Commits per-item to prevent Amazon-side changes from being
        rolled back if a later item fails.
        """
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
            if items:
                logger.info("Monitor loop: %d active items to check", len(items))

            now = datetime.now(timezone.utc)
            for item in items:
                interval = self._effective_interval(item)
                if item.last_checked_at:
                    last = item.last_checked_at if item.last_checked_at.tzinfo else item.last_checked_at.replace(tzinfo=timezone.utc)
                    if (now - last).total_seconds() < interval:
                        continue
                try:
                    await self._check_item(item, db)
                    # Per-item commit: ensures Amazon-side changes (delist etc.)
                    # are persisted even if a later item fails
                    db.commit()
                except Exception as e:
                    logger.warning(
                        "Failed to check item %s (%s): %s",
                        item.auction_id, item.title[:30], e,
                    )
                    db.rollback()

            # Auto-cleanup ended items (7 days after ending)
            self._cleanup_ended_items(db, now)

            # Expire old DealAlerts (7+ days since notification)
            self._expire_old_alerts(db, now)

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
            item.last_checked_at = datetime.now(timezone.utc)
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
        old_win_price = item.win_price
        item.title = data.title
        item.current_price = data.current_price
        item.win_price = data.win_price
        # Sync buy_now_price and estimated_win_price from win_price
        # (page scraper returns buy_now_price=0; win_price is the actual BIN price)
        if data.win_price:
            item.buy_now_price = data.win_price
            item.estimated_win_price = data.win_price
        item.bid_count = data.bid_count
        item.end_time = data.end_time
        item.status = data.status
        item.last_checked_at = datetime.now(timezone.utc)
        item.updated_at = datetime.now(timezone.utc)

        # Sync DealAlert prices when Yahoo price changes
        if data.win_price and data.win_price != old_win_price:
            self._sync_deal_alert_prices(item.auction_id, data.win_price, db)

        # Stop monitoring ended items
        if data.status != "active":
            item.is_monitoring_active = False
            if item.ended_at is None:
                item.ended_at = datetime.now(timezone.utc)
            logger.info("Item %s ended (%s), stopping monitor", item.auction_id, data.status)
            # Expire corresponding DealAlerts (both active and listed)
            expired_count = (
                db.query(DealAlert)
                .filter(
                    DealAlert.yahoo_auction_id == item.auction_id,
                    DealAlert.status.in_(["active", "listed"]),
                )
                .update({"status": "expired"}, synchronize_session="fetch")
            )
            if expired_count:
                logger.info("Expired %d DealAlert(s) for ended auction %s", expired_count, item.auction_id)

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
    def _sync_deal_alert_prices(
        auction_id: str, new_yahoo_price: int, db: Session,
    ) -> None:
        """Update DealAlert yahoo_price and recalculate profit when Yahoo price changes."""
        alerts = (
            db.query(DealAlert)
            .filter(
                DealAlert.yahoo_auction_id == auction_id,
                DealAlert.status.in_(["active", "listed"]),
            )
            .all()
        )
        for alert in alerts:
            old_price = alert.yahoo_price
            if old_price == new_yahoo_price:
                continue
            alert.yahoo_price = new_yahoo_price
            # Recalculate profit:
            # total_cost = yahoo_price + yahoo_shipping + forwarding_cost + system_fee(100)
            system_fee = settings.deal_system_fee
            total_cost = new_yahoo_price + alert.yahoo_shipping + alert.forwarding_cost + system_fee
            amazon_fee = int(alert.sell_price * alert.amazon_fee_pct / 100)
            alert.gross_profit = alert.sell_price - total_cost - amazon_fee
            alert.gross_margin_pct = round(
                (alert.gross_profit / alert.sell_price * 100) if alert.sell_price > 0 else 0.0, 1,
            )
            logger.info(
                "DealAlert %d price synced: %s ¥%d→¥%d (profit ¥%d→¥%d)",
                alert.id, auction_id, old_price, new_yahoo_price,
                alert.gross_profit - (new_yahoo_price - old_price), alert.gross_profit,
            )

    @staticmethod
    def _effective_interval(item: MonitoredItem) -> float:
        """Smart interval: shorten as end_time approaches."""
        if not item.auto_adjust_interval or not item.end_time:
            return item.check_interval_seconds

        now = datetime.now(timezone.utc)
        # Ensure end_time is timezone-aware (SQLite stores naive UTC)
        end = item.end_time if item.end_time.tzinfo else item.end_time.replace(tzinfo=timezone.utc)
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
        """Auto-delete ended MonitoredItems after 7 days.

        Keeps ended items visible for user review. Only auto-cleans
        items that have been ended for 7+ days (fallback cleanup).
        Skips relist candidates (ended_no_winner with ASIN within check window).
        """
        cutoff = now - timedelta(days=7)
        relist_cutoff = now - timedelta(days=settings.relist_check_max_days)
        stale = (
            db.query(MonitoredItem)
            .filter(
                MonitoredItem.status.like("ended_%"),
                MonitoredItem.amazon_listing_status != "active",
                MonitoredItem.amazon_listing_status != "error",
                MonitoredItem.updated_at < cutoff,
            )
            .all()
        )
        # Protect relist candidates from cleanup
        if settings.relist_check_enabled:
            stale = [
                item for item in stale
                if not (
                    item.status == "ended_no_winner"
                    and item.amazon_asin
                    and item.ended_at
                    and item.ended_at > relist_cutoff
                )
            ]
        if stale:
            for item in stale:
                logger.info(
                    "Auto-cleanup: removing old ended item %s (%s, updated %s)",
                    item.auction_id, item.status, item.updated_at,
                )
                db.delete(item)
            logger.info("Auto-cleanup: removed %d old ended items", len(stale))

    @staticmethod
    def _expire_old_alerts(db: Session, now: datetime) -> None:
        """Expire DealAlerts older than 7 days.

        Yahoo auctions typically last 1-7 days. Alerts older than 7 days
        are almost certainly ended, so mark them as expired to remove
        from the auto-scan page without extra scraping.
        """
        cutoff = now - timedelta(days=7)
        expired_count = (
            db.query(DealAlert)
            .filter(
                DealAlert.status.in_(["active", "listed"]),
                DealAlert.notified_at < cutoff,
            )
            .update({"status": "expired"}, synchronize_session="fetch")
        )
        if expired_count:
            logger.info("Expired %d old DealAlert(s) (7+ days)", expired_count)

    # ------------------------------------------------------------------
    # Auto-relist detection（自動再出品検知）
    # ------------------------------------------------------------------

    async def _check_relist_candidates(self) -> None:
        """Check ended_no_winner items for Yahoo auto-relist (自動再出品).

        If a previously ended auction becomes active again:
        1. Update MonitoredItem status back to active
        2. Resume monitoring
        3. Send Discord notification (user can re-list from UI)
        """
        import asyncio

        db: Session = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(days=settings.relist_check_max_days)

            candidates = (
                db.query(MonitoredItem)
                .filter(
                    MonitoredItem.status == "ended_no_winner",
                    MonitoredItem.amazon_asin.isnot(None),
                    MonitoredItem.amazon_listing_status == "delisted",
                    MonitoredItem.ended_at.isnot(None),
                    MonitoredItem.ended_at > cutoff,
                )
                .all()
            )
            if not candidates:
                return

            logger.info(
                "Relist check: %d candidate(s) within %d-day window",
                len(candidates), settings.relist_check_max_days,
            )

            detected = 0
            for item in candidates:
                try:
                    data = await self.scraper.fetch_auction(item.auction_id)
                    if not data or data.status != "active":
                        await asyncio.sleep(0.2)
                        continue

                    # --- Relist detected! ---
                    logger.info(
                        "Auto-relist detected: %s (%s)",
                        item.auction_id, item.title[:40],
                    )

                    # Refresh item data from Yahoo
                    item.title = data.title
                    item.current_price = data.current_price
                    item.win_price = data.win_price
                    if data.win_price:
                        item.buy_now_price = data.win_price
                        item.estimated_win_price = data.win_price
                    item.bid_count = data.bid_count
                    item.end_time = data.end_time
                    item.status = "active"
                    item.is_monitoring_active = True
                    item.last_checked_at = now
                    item.updated_at = now
                    item.ended_at = None
                    item.relist_count = (item.relist_count or 0) + 1

                    db.add(StatusHistory(
                        item_id=item.id,
                        auction_id=item.auction_id,
                        change_type="status_change",
                        old_status="ended_no_winner",
                        new_status="active",
                    ))

                    # Profitability info for notification
                    sell_price = item.amazon_price or 0
                    yahoo_price = item.estimated_win_price or item.buy_now_price or item.win_price or 0
                    shipping = item.shipping_cost or 0
                    forwarding = item.forwarding_cost or settings.deal_forwarding_cost
                    fee_pct = item.amazon_fee_pct or settings.deal_amazon_fee_pct
                    amazon_fee = int(sell_price * fee_pct / 100) if sell_price else 0
                    total_cost = yahoo_price + shipping + forwarding + settings.deal_system_fee + amazon_fee
                    gross_profit = sell_price - total_cost if sell_price else 0
                    margin_pct = (gross_profit / sell_price * 100) if sell_price > 0 else 0.0

                    db.commit()
                    detected += 1

                    # Discord notification: inform user to re-list from UI
                    await self._notify_relist_detected(item, gross_profit, margin_pct)

                    await asyncio.sleep(0.3)
                except Exception as e:
                    logger.warning(
                        "Relist check failed for %s: %s", item.auction_id, e,
                    )
                    db.rollback()

            if detected:
                logger.info("Relist check: %d item(s) detected as re-listed", detected)

        except Exception as e:
            logger.exception("Error in relist check: %s", e)
            db.rollback()
        finally:
            db.close()

    async def _notify_relist_detected(
        self,
        item: MonitoredItem,
        gross_profit: int,
        margin_pct: float,
    ) -> None:
        """Send Discord notification when a Yahoo re-list is detected.

        User can re-list on Amazon from the UI (detail page or keywords page).
        """
        from ..notifier.webhook import send_webhook

        webhook_url = settings.webhook_url
        if not webhook_url:
            return

        yahoo_url = f"https://auctions.yahoo.co.jp/jp/auction/{item.auction_id}"
        detail_url = f"https://yafuama.fly.dev/items/{item.auction_id}"

        profit_str = f"¥{gross_profit:,} ({margin_pct:.1f}%)" if item.amazon_price else "価格未設定"
        color = 0x00CC66 if margin_pct >= settings.relist_min_margin_pct else 0xFF9800

        payload = {
            "embeds": [{
                "title": f"再出品検知: {(item.title or '')[:80]}",
                "url": detail_url,
                "color": color,
                "fields": [
                    {"name": "Yahoo価格", "value": f"¥{item.current_price:,}", "inline": True},
                    {"name": "Amazon売価", "value": f"¥{item.amazon_price:,}" if item.amazon_price else "-", "inline": True},
                    {"name": "想定利益", "value": profit_str, "inline": True},
                    {"name": "再出品回数", "value": str(item.relist_count), "inline": True},
                ],
                "footer": {"text": "ヤフアマ | 詳細ページから再出品できます"},
            }],
        }

        try:
            await send_webhook(webhook_url, payload, webhook_type=settings.webhook_type)
        except Exception as e:
            logger.warning("Relist detection webhook failed: %s", e)

    async def _retry_failed_amazon_deletions(self) -> None:
        """Retry Amazon listing deletion for ended items where deletion failed.

        Covers two cases:
        - listing_status='error': API call failed previously
        - ended item still has amazon_sku + listing_status='active': deletion never attempted

        Processes at most 5 items per cycle to avoid API throttling.
        """
        from ..amazon import AmazonApiError
        from ..main import app_state

        sp_client = app_state.get("sp_api")
        if not sp_client:
            return

        db: Session = SessionLocal()
        try:
            # Find ended items that still have an Amazon listing (limit 5)
            stuck_items = (
                db.query(MonitoredItem)
                .filter(
                    MonitoredItem.status.like("ended_%"),
                    MonitoredItem.amazon_sku.isnot(None),
                )
                .limit(5)
                .all()
            )
            if not stuck_items:
                return

            logger.info(
                "Amazon delete retry: %d ended item(s) still have Amazon listings",
                len(stuck_items),
            )

            deleted_count = 0
            for item in stuck_items:
                try:
                    seller_id = settings.sp_api_seller_id
                    await sp_client.delete_listing(seller_id, item.amazon_sku)
                    old_sku = item.amazon_sku
                    item.amazon_sku = None
                    item.amazon_listing_status = "delisted"
                    item.amazon_last_synced_at = None
                    item.updated_at = datetime.now(timezone.utc)
                    db.add(StatusHistory(
                        item_id=item.id,
                        auction_id=item.auction_id,
                        change_type="amazon_delist_auto",
                        old_status=old_sku,
                    ))
                    deleted_count += 1
                    logger.info(
                        "Amazon delete retry: deleted SKU=%s for ended auction %s",
                        old_sku, item.auction_id,
                    )
                except AmazonApiError as e:
                    logger.warning(
                        "Amazon delete retry: failed for SKU=%s (%s): %s",
                        item.amazon_sku, item.auction_id, e,
                    )
                except Exception as e:
                    logger.warning(
                        "Amazon delete retry: unexpected error for SKU=%s (%s): %s",
                        item.amazon_sku, item.auction_id, e,
                    )

            if deleted_count:
                logger.info("Amazon delete retry: successfully deleted %d listing(s)", deleted_count)
            db.commit()
        except Exception as e:
            logger.exception("Error in Amazon delete retry: %s", e)
            db.rollback()
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Data retention cleanup（古いレコードの物理削除）
    # ------------------------------------------------------------------

    async def _data_retention_cleanup(self) -> None:
        """Delete old records to prevent unbounded database growth.

        Retention policy:
        - DealAlert (expired/rejected): 90 days
        - StatusHistory: 90 days
        - NotificationLog: 30 days
        - KeywordCandidate (resolved): 30 days
        - DiscoveryLog: 90 days
        - AmazonOrder: 180 days
        """
        db: Session = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            cutoff_180d = now - timedelta(days=180)
            cutoff_90d = now - timedelta(days=90)
            cutoff_30d = now - timedelta(days=30)
            total_deleted = 0

            # DealAlerts: delete expired/rejected older than 90 days
            count = (
                db.query(DealAlert)
                .filter(
                    DealAlert.status.in_(["expired", "rejected"]),
                    DealAlert.notified_at < cutoff_90d,
                )
                .delete(synchronize_session=False)
            )
            if count:
                logger.info("Data retention: deleted %d old DealAlert(s)", count)
                total_deleted += count

            # StatusHistory: delete older than 90 days
            count = (
                db.query(StatusHistory)
                .filter(StatusHistory.recorded_at < cutoff_90d)
                .delete(synchronize_session=False)
            )
            if count:
                logger.info("Data retention: deleted %d old StatusHistory records", count)
                total_deleted += count

            # NotificationLog: delete older than 30 days
            count = (
                db.query(NotificationLog)
                .filter(NotificationLog.sent_at < cutoff_30d)
                .delete(synchronize_session=False)
            )
            if count:
                logger.info("Data retention: deleted %d old NotificationLog records", count)
                total_deleted += count

            # KeywordCandidate: delete resolved (not pending) older than 30 days
            count = (
                db.query(KeywordCandidate)
                .filter(
                    KeywordCandidate.status != "pending",
                    KeywordCandidate.created_at < cutoff_30d,
                )
                .delete(synchronize_session=False)
            )
            if count:
                logger.info("Data retention: deleted %d old KeywordCandidate(s)", count)
                total_deleted += count

            # DiscoveryLog: delete older than 90 days
            count = (
                db.query(DiscoveryLog)
                .filter(DiscoveryLog.started_at < cutoff_90d)
                .delete(synchronize_session=False)
            )
            if count:
                logger.info("Data retention: deleted %d old DiscoveryLog records", count)
                total_deleted += count

            # AmazonOrder: delete older than 180 days
            count = (
                db.query(AmazonOrder)
                .filter(AmazonOrder.created_at < cutoff_180d)
                .delete(synchronize_session=False)
            )
            if count:
                logger.info("Data retention: deleted %d old AmazonOrder(s)", count)
                total_deleted += count

            db.commit()

            if total_deleted:
                logger.info("Data retention cleanup: %d total records deleted", total_deleted)
            else:
                logger.info("Data retention cleanup: no old records to delete")

        except Exception as e:
            logger.exception("Error in data retention cleanup: %s", e)
            db.rollback()
        finally:
            db.close()
