"""APScheduler-based monitoring loop with smart interval adjustment."""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal
from ..models import DealAlert, MonitoredItem, NotificationLog, StatusHistory
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
            self._expire_ended_alerts,
            "interval",
            seconds=1800,  # 30分ごと
            id="alert_cleanup",
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
                except Exception as e:
                    logger.warning(
                        "Failed to check item %s (%s): %s",
                        item.auction_id, item.title[:30], e,
                    )

            # Auto-cleanup ended items (3 days after ending)
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
            # Expire corresponding DealAlerts
            expired_count = (
                db.query(DealAlert)
                .filter(
                    DealAlert.yahoo_auction_id == item.auction_id,
                    DealAlert.status == "active",
                )
                .update({"status": "expired"})
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
                DealAlert.status == "active",
                DealAlert.notified_at < cutoff,
            )
            .update({"status": "expired"})
        )
        if expired_count:
            logger.info("Expired %d old DealAlert(s) (7+ days)", expired_count)

    async def _expire_ended_alerts(self) -> None:
        """Check active/listed DealAlerts: expire ended auctions, sync prices."""
        db: Session = SessionLocal()
        try:
            live_alerts = (
                db.query(DealAlert)
                .filter(DealAlert.status.in_(["active", "listed"]))
                .all()
            )
            if not live_alerts:
                return

            # Group by auction_id to avoid duplicate fetches
            alerts_by_auction: dict[str, list[DealAlert]] = {}
            for alert in live_alerts:
                alerts_by_auction.setdefault(alert.yahoo_auction_id, []).append(alert)

            logger.info(
                "Alert sync: checking %d auctions for %d alerts",
                len(alerts_by_auction), len(live_alerts),
            )

            expired_count = 0
            synced_count = 0
            for auction_id, alerts in alerts_by_auction.items():
                try:
                    data = await self.scraper.fetch_auction(auction_id)
                    if not data:
                        continue
                    if data.status != "active":
                        for alert in alerts:
                            if alert.status == "active":
                                alert.status = "expired"
                                expired_count += 1
                    else:
                        # Sync prices: use win_price (即決) if available, else current_price
                        live_price = data.win_price if data.win_price and data.win_price > 0 else data.current_price
                        if live_price and live_price > 0:
                            for alert in alerts:
                                if alert.yahoo_price != live_price:
                                    self._sync_deal_alert_prices(auction_id, live_price, db)
                                    synced_count += 1
                                    break  # _sync handles all alerts for this auction
                except Exception as e:
                    logger.warning("Alert sync: failed to check %s: %s", auction_id, e)

            if expired_count:
                logger.info("Alert sync: expired %d alert(s)", expired_count)
            if synced_count:
                logger.info("Alert sync: price-synced %d auction(s)", synced_count)
            db.commit()
        except Exception as e:
            logger.exception("Error in alert cleanup: %s", e)
            db.rollback()
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Auto-relist detection（自動再出品検知）
    # ------------------------------------------------------------------

    async def _check_relist_candidates(self) -> None:
        """Check ended_no_winner items for Yahoo auto-relist (自動再出品).

        If a previously ended auction becomes active again:
        1. Recalculate profitability with current Yahoo price
        2. If still profitable → auto-create Amazon listing
        3. Resume monitoring
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

            relisted = 0
            skipped_profit = 0
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

                    # Profitability check & auto-list
                    listed = await self._auto_relist_amazon(item, db)
                    if listed:
                        relisted += 1
                    else:
                        skipped_profit += 1

                    # Commit per item to prevent orphaned Amazon listings on crash
                    db.commit()

                    await asyncio.sleep(0.3)
                except Exception as e:
                    logger.warning(
                        "Relist check failed for %s: %s", item.auction_id, e,
                    )
                    db.rollback()

            if relisted:
                logger.info("Relist check: %d item(s) relisted on Amazon", relisted)
            if skipped_profit:
                logger.info("Relist check: %d item(s) skipped (profit below threshold)", skipped_profit)

        except Exception as e:
            logger.exception("Error in relist check: %s", e)
            db.rollback()
        finally:
            db.close()

    async def _auto_relist_amazon(self, item: MonitoredItem, db: Session) -> bool:
        """Recalculate profitability and auto-create Amazon listing.

        Returns True if listing was created, False if skipped.
        """
        import asyncio

        from ..amazon import AmazonApiError
        from ..amazon.pricing import generate_sku
        from ..amazon.shipping import get_pattern_by_key
        from ..main import app_state

        sp_client = app_state.get("sp_api")
        if not sp_client:
            logger.warning("Auto-relist: SP-API not available")
            return False

        # Prices
        sell_price = item.amazon_price or 0
        yahoo_price = item.estimated_win_price or item.buy_now_price or item.win_price or 0
        if sell_price <= 0 or yahoo_price <= 0:
            logger.info("Auto-relist: no price data for %s, skipping", item.auction_id)
            return False

        # Profit calculation
        shipping = item.shipping_cost or 0
        forwarding = item.forwarding_cost or settings.deal_forwarding_cost
        fee_pct = item.amazon_fee_pct or settings.deal_amazon_fee_pct
        amazon_fee = int(sell_price * fee_pct / 100)
        total_cost = yahoo_price + shipping + forwarding + settings.deal_system_fee + amazon_fee
        gross_profit = sell_price - total_cost
        margin_pct = (gross_profit / sell_price * 100) if sell_price > 0 else 0.0

        logger.info(
            "Auto-relist profit check %s: sell=¥%d yahoo=¥%d profit=¥%d margin=%.1f%%",
            item.auction_id, sell_price, yahoo_price, gross_profit, margin_pct,
        )

        if margin_pct < settings.relist_min_margin_pct:
            logger.info(
                "Auto-relist: %s margin %.1f%% < %.1f%%, skipping",
                item.auction_id, margin_pct, settings.relist_min_margin_pct,
            )
            db.add(StatusHistory(
                item_id=item.id, auction_id=item.auction_id,
                change_type="auto_relist_skip",
                new_status=f"margin {margin_pct:.1f}% < {settings.relist_min_margin_pct}%",
            ))
            return False

        if gross_profit < settings.relist_min_profit:
            logger.info(
                "Auto-relist: %s profit ¥%d < ¥%d, skipping",
                item.auction_id, gross_profit, settings.relist_min_profit,
            )
            db.add(StatusHistory(
                item_id=item.id, auction_id=item.auction_id,
                change_type="auto_relist_skip",
                new_status=f"profit ¥{gross_profit} < ¥{settings.relist_min_profit}",
            ))
            return False

        # --- Profitable → create Amazon listing ---
        condition = item.amazon_condition or "used_very_good"
        suffix = datetime.now(timezone.utc).strftime("%y%m%d%H%M")
        sku = f"{generate_sku(item.auction_id)}-R{suffix}"

        pattern = get_pattern_by_key(item.amazon_shipping_pattern or "2_3_days")
        if not pattern:
            pattern = get_pattern_by_key("2_3_days")
        lead_time = pattern.lead_time_days

        attributes = {
            "condition_type": [{"value": condition}],
            "purchasable_offer": [{
                "marketplace_id": settings.sp_api_marketplace,
                "currency": "JPY",
                "our_price": [{"schedule": [{"value_with_tax": sell_price}]}],
            }],
            "fulfillment_availability": [
                {"fulfillment_channel_code": "DEFAULT", "quantity": 1}
            ],
            "merchant_suggested_asin": [{
                "value": item.amazon_asin,
                "marketplace_id": settings.sp_api_marketplace,
            }],
            "merchant_shipping_group": [{"value": settings.shipping_template_id}],
            "lead_time_to_ship_max_days": [{"value": lead_time}],
        }
        if item.amazon_condition_note:
            attributes["condition_note"] = [
                {"value": item.amazon_condition_note, "language_tag": "ja_JP"}
            ]

        try:
            product_type = await sp_client.get_product_type(item.amazon_asin)

            try:
                await sp_client.create_listing(
                    settings.sp_api_seller_id, sku, product_type, attributes,
                    offer_only=True,
                )
            except AmazonApiError as e:
                if "INVALID" in str(e):
                    attributes.pop("lead_time_to_ship_max_days", None)
                    await sp_client.create_listing(
                        settings.sp_api_seller_id, sku, product_type, attributes,
                        offer_only=True,
                    )
                else:
                    raise

            await asyncio.sleep(3)

            # Activation PATCHes
            try:
                await sp_client.patch_listing_price(
                    settings.sp_api_seller_id, sku, sell_price,
                )
            except AmazonApiError as e:
                logger.warning("Auto-relist: price PATCH failed for %s: %s", sku, e)
            try:
                await sp_client.patch_listing_quantity(
                    settings.sp_api_seller_id, sku, 1,
                )
            except AmazonApiError as e:
                logger.warning("Auto-relist: quantity PATCH failed for %s: %s", sku, e)
            # Try lead_time PATCH (often rejected by SP-API, user sets manually)
            try:
                await sp_client.patch_listing_lead_time(
                    settings.sp_api_seller_id, sku, lead_time,
                )
            except AmazonApiError:
                logger.info("Auto-relist: lead_time PATCH failed for %s (manual setup needed)", sku)

            # Update item
            item.amazon_sku = sku
            item.amazon_listing_status = "active"
            item.amazon_last_synced_at = datetime.now(timezone.utc)
            item.amazon_margin_pct = round(margin_pct, 1)
            item.seller_central_checklist = ""  # チェックリストリセット

            db.add(StatusHistory(
                item_id=item.id, auction_id=item.auction_id,
                change_type="amazon_listing",
                new_status=f"auto_relist:{sku} ¥{sell_price} margin {margin_pct:.1f}%",
            ))

            logger.info(
                "Auto-relist SUCCESS: %s SKU=%s ¥%d margin=%.1f%% (relist #%d)",
                item.auction_id, sku, sell_price, margin_pct, item.relist_count,
            )

            # Discord通知: リードタイム手動設定のリマインダー
            await self._notify_auto_relist(item, sku, sell_price, gross_profit, margin_pct, lead_time)

            return True

        except AmazonApiError as e:
            logger.error("Auto-relist SP-API error for %s: %s", item.auction_id, e)
            db.add(StatusHistory(
                item_id=item.id, auction_id=item.auction_id,
                change_type="auto_relist_error",
                new_status=str(e)[:200],
            ))
            return False

    async def _notify_auto_relist(
        self,
        item: MonitoredItem,
        sku: str,
        sell_price: int,
        gross_profit: int,
        margin_pct: float,
        lead_time: int,
    ) -> None:
        """Send Discord notification for auto-relist with lead_time reminder."""
        from ..notifier.webhook import send_webhook

        webhook_url = settings.webhook_url
        if not webhook_url:
            return

        sc_url = (
            f"https://sellercentral.amazon.co.jp/inventory"
            f"?tbla_myitable=sort:%7B%22sortOrder%22%3A%22DESCENDING%22%7D;search:{sku}"
        )

        payload = {
            "content": "@here 自動再出品完了！リードタイムを確認してください",
            "embeds": [
                {
                    "title": "自動再出品: " + (item.title or "")[:80],
                    "url": item.url,
                    "color": 0x00CC66,
                    "fields": [
                        {"name": "SKU", "value": sku, "inline": True},
                        {"name": "販売価格", "value": f"¥{sell_price:,}", "inline": True},
                        {"name": "利益", "value": f"¥{gross_profit:,} ({margin_pct:.1f}%)", "inline": True},
                        {"name": "再出品回数", "value": str(item.relist_count), "inline": True},
                        {"name": "設定リードタイム", "value": f"{lead_time}日", "inline": True},
                    ],
                    "footer": {"text": "ヤフアマ Auto-Relist"},
                },
                {
                    "title": ">>> セラーセントラルでリードタイムを確認 <<<",
                    "url": sc_url,
                    "color": 0xFF9800,
                    "description": (
                        "SP-APIではリードタイムが反映されないことがあります。\n"
                        f"セラーセントラルで **{lead_time}日** に設定してください。"
                    ),
                },
            ],
        }

        try:
            await send_webhook(webhook_url, payload, webhook_type=settings.webhook_type)
        except Exception as e:
            logger.warning("Auto-relist webhook failed: %s", e)

    async def _retry_failed_amazon_deletions(self) -> None:
        """Retry Amazon listing deletion for ended items where deletion failed.

        Covers two cases:
        - listing_status='error': API call failed previously
        - ended item still has amazon_sku + listing_status='active': deletion never attempted
        """
        from ..amazon import AmazonApiError
        from ..main import app_state

        sp_client = app_state.get("sp_api")
        if not sp_client:
            return

        db: Session = SessionLocal()
        try:
            # Find ended items that still have an Amazon listing
            stuck_items = (
                db.query(MonitoredItem)
                .filter(
                    MonitoredItem.status.like("ended_%"),
                    MonitoredItem.amazon_sku.isnot(None),
                )
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
