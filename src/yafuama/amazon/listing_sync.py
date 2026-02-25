"""Periodic sync: detect listings deleted or price-changed from Seller Central."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal
from ..models import MonitoredItem, StatusHistory
from . import AmazonApiError
from .client import SpApiClient

logger = logging.getLogger(__name__)


class ListingSyncChecker:
    """Periodically verify Amazon listings still exist and sync price changes.

    If a listing was deleted directly from Seller Central,
    update the local DB to reflect that.

    If the price was changed on Seller Central,
    sync it back to the local DB and recalculate profit margins.
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
            price_synced = 0
            for item in items:
                listing_data = await self._fetch_listing(item.amazon_sku)

                if listing_data is None:
                    # Listing not found
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
                else:
                    self._fail_counts.pop(item.amazon_sku, None)

                    # Check for price changes
                    if self._sync_price(item, listing_data, db):
                        price_synced += 1

                await asyncio.sleep(0.2)  # Rate limit protection

            if cleaned or price_synced:
                db.commit()

            if cleaned:
                logger.info("Listing sync: cleaned %d stale listings", cleaned)
            if price_synced:
                logger.info("Listing sync: synced %d price changes from Seller Central", price_synced)
            if not cleaned and not price_synced:
                logger.info("Listing sync: all listings OK")

        except Exception as e:
            logger.exception("Listing sync error: %s", e)
            db.rollback()
        finally:
            db.close()

    def _sync_price(self, item: MonitoredItem, listing_data: dict, db: Session) -> bool:
        """Check if Amazon price differs from local DB and sync if needed.

        Returns True if a price change was detected and synced.
        """
        amazon_price = self._extract_price(listing_data)
        if amazon_price is None or amazon_price <= 0:
            return False

        local_price = item.amazon_price or 0
        if amazon_price == local_price:
            return False

        # Price changed on Seller Central — sync it
        old_price = local_price
        item.amazon_price = amazon_price

        # Recalculate profit margin based on new price
        cost = item.estimated_win_price + item.shipping_cost
        if cost > 0 and amazon_price > 0:
            fee = amazon_price * (item.amazon_fee_pct / 100.0)
            forwarding = item.forwarding_cost or settings.deal_forwarding_cost
            profit = amazon_price - cost - fee - forwarding - settings.deal_system_fee
            margin_pct = round(profit / amazon_price * 100, 1) if amazon_price > 0 else 0.0
            # Update margin to reflect actual Seller Central price
            # Back-calculate: margin_pct = 1 - (cost / price) - fee_pct/100
            if amazon_price > 0:
                actual_margin = (1.0 - cost / amazon_price - item.amazon_fee_pct / 100.0) * 100.0
                item.amazon_margin_pct = round(actual_margin, 1)

        item.amazon_last_synced_at = datetime.now(timezone.utc)
        item.updated_at = datetime.now(timezone.utc)

        db.add(StatusHistory(
            item_id=item.id, auction_id=item.auction_id,
            change_type="price_change",
            old_price=old_price,
            new_price=amazon_price,
            old_status="セラーセントラルで価格変更検知",
        ))

        logger.info(
            "Price sync: %s (SKU=%s) ¥%d → ¥%d (margin: %.1f%%)",
            item.auction_id, item.amazon_sku, old_price, amazon_price,
            item.amazon_margin_pct,
        )
        return True

    def _extract_price(self, listing_data: dict) -> int | None:
        """Extract the current price from SP-API listing response."""
        try:
            # SP-API getListingsItem response structure
            summaries = listing_data.get("summaries") or []
            for summary in summaries:
                price_info = summary.get("price")
                if price_info:
                    amount = price_info.get("amount")
                    if amount is not None:
                        return int(float(amount))

            # Alternative: check offers/attributes
            attributes = listing_data.get("attributes") or {}
            our_price = attributes.get("purchasable_offer") or attributes.get("our_price")
            if our_price and isinstance(our_price, list):
                for price_entry in our_price:
                    schedule = price_entry.get("schedule") or []
                    for s in schedule:
                        val = s.get("value_with_tax") or s.get("value")
                        if val is not None:
                            return int(float(val))

        except (KeyError, TypeError, ValueError) as e:
            logger.debug("Could not extract price from listing data: %s", e)

        return None

    async def _fetch_listing(self, sku: str) -> dict | None:
        """Fetch listing data from Amazon. Returns None if not found."""
        try:
            result = await self.client.get_listing(self.seller_id, sku)
            return result
        except AmazonApiError as e:
            status_code = getattr(e, "status_code", None)
            if status_code == 404:
                return None
            # Other errors (throttling, 500, etc.) → treat as "exists" to be safe
            logger.warning("Listing fetch error for SKU %s: %s", sku, e)
            return {}  # Empty dict = exists but couldn't read details
