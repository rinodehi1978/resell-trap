"""Amazon order monitor: polls SP-API Orders API and sends Discord notifications."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal
from ..models import MonitoredItem
from ..notifier.webhook import send_webhook
from . import AmazonApiError
from .client import SpApiClient

logger = logging.getLogger(__name__)

# Seller Central order detail URL
SELLER_CENTRAL_ORDER_URL = (
    "https://sellercentral.amazon.co.jp/orders-v3/order/{order_id}"
)


class OrderMonitor:
    """Polls SP-API Orders API for new orders and sends Discord notifications.

    Tracks the last check timestamp in memory.  On first run, starts from
    the current UTC time so only genuinely new orders trigger notifications.
    """

    def __init__(
        self,
        client: SpApiClient,
        webhook_url: str,
        webhook_type: str = "discord",
    ) -> None:
        self.client = client
        self.webhook_url = webhook_url
        self.webhook_type = webhook_type
        # Set to "now" so the first poll only catches orders created after startup
        self._last_checked_at: str = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        # Track seen order IDs to avoid duplicate notifications
        self._seen_order_ids: set[str] = set()

    async def check_orders(self) -> None:
        """Main entry point called by the scheduler."""
        try:
            orders = await self.client.get_new_orders(self._last_checked_at)
        except AmazonApiError as e:
            logger.warning("Order monitor: SP-API error: %s", e)
            return
        except Exception as e:
            logger.exception("Order monitor: unexpected error fetching orders: %s", e)
            return

        # Update the checkpoint *before* processing so we don't re-fetch
        # the same window on next cycle even if notification fails.
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        new_orders = [
            o for o in orders
            if o.get("AmazonOrderId") not in self._seen_order_ids
        ]

        if not new_orders:
            logger.debug("Order monitor: no new orders since %s", self._last_checked_at)
            self._last_checked_at = now
            return

        logger.info("Order monitor: %d new order(s) found", len(new_orders))

        # Look up product info from MonitoredItem table
        db: Session = SessionLocal()
        try:
            for order in new_orders:
                order_id = order.get("AmazonOrderId", "unknown")
                self._seen_order_ids.add(order_id)
                await self._notify_order(order, db)
        except Exception as e:
            logger.exception("Order monitor: error processing orders: %s", e)
        finally:
            db.close()

        self._last_checked_at = now

        # Prevent unbounded memory growth: keep only recent 500 order IDs
        if len(self._seen_order_ids) > 500:
            self._seen_order_ids = set(list(self._seen_order_ids)[-200:])

    async def _notify_order(self, order: dict, db: Session) -> None:
        """Build and send a Discord notification for a single order."""
        order_id = order.get("AmazonOrderId", "unknown")
        order_status = order.get("OrderStatus", "unknown")
        purchase_date = order.get("PurchaseDate", "")
        order_total = order.get("OrderTotal", {})
        total_amount = order_total.get("Amount", "?")
        total_currency = order_total.get("CurrencyCode", "JPY")
        num_items = order.get("NumberOfItemsUnshipped", 0)

        # Build Seller Central link
        sc_url = SELLER_CENTRAL_ORDER_URL.format(order_id=order_id)

        # Try to find matching MonitoredItem by looking up order items
        # (Order-level data doesn't include SKU; we show what we can)
        product_info = self._lookup_product_info(order, db)

        # Format the notification
        if self.webhook_type == "discord":
            payload = self._build_discord_payload(
                order_id=order_id,
                order_status=order_status,
                purchase_date=purchase_date,
                total_amount=total_amount,
                total_currency=total_currency,
                num_items=num_items,
                sc_url=sc_url,
                product_info=product_info,
            )
        else:
            # Fallback for non-discord webhooks
            message = (
                f"[Amazon注文通知]\n"
                f"注文ID: {order_id}\n"
                f"ステータス: {order_status}\n"
                f"金額: {total_currency} {total_amount}\n"
                f"商品数: {num_items}\n"
                f"セラセン: {sc_url}"
            )
            if product_info:
                message += f"\n商品: {product_info.get('title', '')}"
            payload = {"content": message} if self.webhook_type == "slack" else {"message": message}

        success = await send_webhook(
            self.webhook_url, payload, webhook_type=self.webhook_type
        )
        if success:
            logger.info("Order notification sent for %s", order_id)
        else:
            logger.warning("Failed to send order notification for %s", order_id)

    def _build_discord_payload(
        self,
        *,
        order_id: str,
        order_status: str,
        purchase_date: str,
        total_amount: str,
        total_currency: str,
        num_items: int,
        sc_url: str,
        product_info: dict | None,
    ) -> dict:
        """Build a Discord embed payload for an order notification."""
        # Format purchase date for display
        display_date = purchase_date
        try:
            dt = datetime.fromisoformat(purchase_date.replace("Z", "+00:00"))
            display_date = dt.strftime("%Y-%m-%d %H:%M JST")
        except (ValueError, AttributeError):
            pass

        # Format amount
        if total_currency == "JPY":
            amount_display = f"¥{int(float(total_amount)):,}" if total_amount != "?" else "?"
        else:
            amount_display = f"{total_currency} {total_amount}"

        fields = [
            {"name": "注文ID", "value": f"[{order_id}]({sc_url})", "inline": False},
            {"name": "金額", "value": amount_display, "inline": True},
            {"name": "商品数", "value": str(num_items), "inline": True},
            {"name": "ステータス", "value": order_status, "inline": True},
            {"name": "注文日時", "value": display_date, "inline": False},
        ]

        # Add product info if we found a matching MonitoredItem
        if product_info:
            title = product_info.get("title", "")
            sku = product_info.get("sku", "")
            yahoo_url = product_info.get("yahoo_url", "")
            if title:
                fields.append(
                    {"name": "商品名", "value": title[:200], "inline": False}
                )
            if sku:
                fields.append(
                    {"name": "SKU", "value": sku, "inline": True}
                )
            if yahoo_url:
                fields.append(
                    {"name": "ヤフオク", "value": f"[出品ページ]({yahoo_url})", "inline": True}
                )

        return {
            "content": "@here Amazon新規注文!",
            "embeds": [
                {
                    "title": "Amazon 新規注文通知",
                    "url": sc_url,
                    "color": 0x00AA00,  # Green
                    "fields": fields,
                    "footer": {"text": "ヤフアマ Order Monitor"},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ],
        }

    @staticmethod
    def _lookup_product_info(order: dict, db: Session) -> dict | None:
        """Try to match order to a MonitoredItem via order items.

        The Orders API getOrders response does not include per-item SKU data.
        We attempt a rough match by checking if there is a MonitoredItem with
        an active Amazon listing whose ASIN or SKU relates to this order.

        For a more precise match, getOrderItems would be needed, but that
        requires an additional API call per order (rate limited).  For now,
        we return None if we cannot determine the product.

        If there is only one active MonitoredItem with an Amazon listing,
        we assume it is the relevant product (common for small sellers).
        """
        try:
            active_items = (
                db.query(MonitoredItem)
                .filter(
                    MonitoredItem.amazon_sku.isnot(None),
                    MonitoredItem.amazon_listing_status == "active",
                )
                .all()
            )
            if len(active_items) == 1:
                item = active_items[0]
                return {
                    "title": item.title,
                    "sku": item.amazon_sku,
                    "yahoo_url": item.url,
                    "asin": item.amazon_asin,
                }
        except Exception as e:
            logger.warning("Order monitor: DB lookup failed: %s", e)

        return None
