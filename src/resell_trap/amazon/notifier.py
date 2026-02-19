"""AmazonNotifier: auto-sync Amazon listing quantity on auction status changes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..models import MonitoredItem, StatusHistory
from ..notifier.base import BaseNotifier
from . import AmazonApiError
from .client import SpApiClient

logger = logging.getLogger(__name__)


class AmazonNotifier(BaseNotifier):
    """Reacts to auction status changes and syncs Amazon listing quantity.

    - auction ended (any) -> qty=0 (listing inactive)
    - auction back to active (relist) -> qty=1 (listing reactivated)
    - Items without amazon_sku are silently skipped.
    """

    def __init__(self, client: SpApiClient, seller_id: str) -> None:
        self.client = client
        self.seller_id = seller_id

    async def notify(self, item: MonitoredItem, change: StatusHistory) -> bool:
        if change.change_type != "status_change":
            return True

        if not item.amazon_sku:
            return True

        new_status = change.new_status or ""
        if new_status.startswith("ended_"):
            return await self._set_quantity(item, 0)
        elif new_status == "active":
            return await self._set_quantity(item, 1)

        return True

    async def _set_quantity(self, item: MonitoredItem, quantity: int) -> bool:
        action = "deactivating" if quantity == 0 else "reactivating"
        logger.info(
            "%s Amazon listing for %s (SKU: %s)", action, item.auction_id, item.amazon_sku
        )
        try:
            await self.client.patch_listing_quantity(self.seller_id, item.amazon_sku, quantity)
            item.amazon_listing_status = "inactive" if quantity == 0 else "active"
            item.amazon_last_synced_at = datetime.now(timezone.utc)
            return True
        except AmazonApiError as e:
            logger.error("Failed to update Amazon listing for %s: %s", item.amazon_sku, e)
            item.amazon_listing_status = "error"
            return False

    def format_message(self, item: MonitoredItem, change: StatusHistory) -> str:
        base = super().format_message(item, change)
        if item.amazon_sku:
            base += f"\nAmazon SKU: {item.amazon_sku} | Status: {item.amazon_listing_status}"
        return base
