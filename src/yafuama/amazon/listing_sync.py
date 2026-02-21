"""Periodic sync: detect listings deleted from Seller Central."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal
from ..models import MonitoredItem, StatusHistory
from . import AmazonApiError
from .client import SpApiClient

logger = logging.getLogger(__name__)


class ListingSyncChecker:
    """Periodically verify Amazon listings still exist.

    If a listing was deleted directly from Seller Central,
    update the local DB to reflect that.
    """

    def __init__(self, client: SpApiClient) -> None:
        self.client = client
        self.seller_id = settings.sp_api_seller_id
        # Track consecutive failures per SKU to avoid false positives
        self._fail_counts: dict[str, int] = {}

    async def check_all(self) -> None:
        db: Session = SessionLocal()
        try:
            items = (
                db.query(MonitoredItem)
                .filter(
                    MonitoredItem.amazon_sku.isnot(None),
                    MonitoredItem.amazon_listing_status.in_(["active", "inactive"]),
                )
                .all()
            )
            if not items:
                return

            logger.info("Listing sync: checking %d Amazon listings", len(items))
            cleaned = 0
            for item in items:
                still_exists = await self._check_listing(item.amazon_sku)
                if still_exists:
                    self._fail_counts.pop(item.amazon_sku, None)
                else:
                    count = self._fail_counts.get(item.amazon_sku, 0) + 1
                    self._fail_counts[item.amazon_sku] = count
                    if count >= 2:
                        # 2回連続で見つからない → セラセン側で削除された
                        logger.warning(
                            "Listing gone from Amazon: SKU=%s (auction=%s) — clearing local state",
                            item.amazon_sku, item.auction_id,
                        )
                        old_sku = item.amazon_sku
                        item.amazon_sku = None
                        item.amazon_listing_status = "delisted"
                        item.amazon_last_synced_at = None
                        item.updated_at = datetime.now(timezone.utc)
                        db.add(StatusHistory(
                            item_id=item.id, auction_id=item.auction_id,
                            change_type="amazon_delist",
                            old_status=old_sku,
                            new_status="セラーセントラルで削除検知",
                        ))
                        self._fail_counts.pop(old_sku, None)
                        cleaned += 1

            if cleaned:
                db.commit()
                logger.info("Listing sync: cleaned %d stale listings", cleaned)
            else:
                logger.info("Listing sync: all listings OK")

        except Exception as e:
            logger.exception("Listing sync error: %s", e)
            db.rollback()
        finally:
            db.close()

    async def _check_listing(self, sku: str) -> bool:
        """Return True if the listing still exists on Amazon."""
        try:
            result = await self.client.get_listing(self.seller_id, sku)
            # If we get a result, listing exists
            return True
        except AmazonApiError as e:
            status_code = getattr(e, "status_code", None)
            if status_code == 404:
                return False
            # Other errors (throttling, 500, etc.) → treat as "exists" to be safe
            logger.warning("Listing check error for SKU %s: %s", sku, e)
            return True
