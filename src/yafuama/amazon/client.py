"""Async wrapper around python-amazon-sp-api (synchronous library)."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any

from sp_api.api import CatalogItems, ListingsItems, ListingsRestrictions
from sp_api.base import Marketplaces, SellingApiException

from ..config import settings
from . import AmazonApiError

logger = logging.getLogger(__name__)


class SpApiClient:
    """Thin async wrapper around python-amazon-sp-api."""

    def __init__(self) -> None:
        self._credentials = {
            "refresh_token": settings.sp_api_refresh_token,
            "lwa_app_id": settings.sp_api_lwa_app_id,
            "lwa_client_secret": settings.sp_api_lwa_client_secret,
            "aws_access_key": settings.sp_api_aws_access_key,
            "aws_secret_key": settings.sp_api_aws_secret_key,
            "role_arn": settings.sp_api_role_arn,
        }
        self._marketplace = Marketplaces.JP
        self._marketplace_id = settings.sp_api_marketplace

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, partial(fn, *args, **kwargs))
            return result.payload
        except SellingApiException as e:
            raise AmazonApiError(str(e), getattr(e, "status_code", None)) from e

    def _catalog_api(self) -> CatalogItems:
        return CatalogItems(
            credentials=self._credentials,
            marketplace=self._marketplace,
        )

    def _listings_api(self) -> ListingsItems:
        return ListingsItems(
            credentials=self._credentials,
            marketplace=self._marketplace,
        )

    def _restrictions_api(self) -> ListingsRestrictions:
        return ListingsRestrictions(
            credentials=self._credentials,
            marketplace=self._marketplace,
        )

    # --- Catalog ---

    async def get_catalog_item(self, asin: str) -> dict:
        api = self._catalog_api()
        return await self._call(
            api.get_catalog_item,
            asin=asin,
            marketplaceIds=[self._marketplace_id],
            includedData=["summaries", "images", "salesRanks"],
        )

    async def search_catalog_items(self, keywords: str, page_size: int = 10) -> list[dict]:
        api = self._catalog_api()
        result = await self._call(
            api.search_catalog_items,
            keywords=keywords,
            marketplaceIds=[self._marketplace_id],
            includedData=["summaries", "images"],
            pageSize=page_size,
        )
        return result.get("items", []) if isinstance(result, dict) else []

    # --- Listings ---

    async def create_listing(
        self, seller_id: str, sku: str, product_type: str, attributes: dict
    ) -> dict:
        api = self._listings_api()
        body = {"productType": product_type, "attributes": attributes}
        return await self._call(
            api.put_listings_item,
            sellerId=seller_id,
            sku=sku,
            marketplaceIds=[self._marketplace_id],
            body=body,
        )

    async def patch_listing_quantity(self, seller_id: str, sku: str, quantity: int) -> dict:
        api = self._listings_api()
        body = {
            "productType": "PRODUCT",
            "patches": [{
                "op": "replace",
                "path": "/attributes/fulfillment_availability",
                "value": [{"fulfillment_channel_code": "DEFAULT", "quantity": quantity}],
            }],
        }
        return await self._call(
            api.patch_listings_item,
            sellerId=seller_id, sku=sku,
            marketplaceIds=[self._marketplace_id], body=body,
        )

    async def patch_listing_price(self, seller_id: str, sku: str, price_jpy: int) -> dict:
        api = self._listings_api()
        body = {
            "productType": "PRODUCT",
            "patches": [{
                "op": "replace",
                "path": "/attributes/purchasable_offer",
                "value": [{
                    "marketplace_id": self._marketplace_id,
                    "currency": "JPY",
                    "our_price": [{"schedule": [{"value_with_tax": price_jpy}]}],
                }],
            }],
        }
        return await self._call(
            api.patch_listings_item,
            sellerId=seller_id, sku=sku,
            marketplaceIds=[self._marketplace_id], body=body,
        )

    async def patch_listing_lead_time(self, seller_id: str, sku: str, days: int) -> dict:
        api = self._listings_api()
        body = {
            "productType": "PRODUCT",
            "patches": [{
                "op": "replace",
                "path": "/attributes/lead_time_to_ship_max_days",
                "value": [{"value": days}],
            }],
        }
        return await self._call(
            api.patch_listings_item,
            sellerId=seller_id, sku=sku,
            marketplaceIds=[self._marketplace_id], body=body,
        )

    async def patch_listing_shipping_group(self, seller_id: str, sku: str, group_name: str) -> dict:
        api = self._listings_api()
        body = {
            "productType": "PRODUCT",
            "patches": [{
                "op": "replace",
                "path": "/attributes/merchant_shipping_group",
                "value": [{"value": group_name}],
            }],
        }
        return await self._call(
            api.patch_listings_item,
            sellerId=seller_id, sku=sku,
            marketplaceIds=[self._marketplace_id], body=body,
        )

    async def get_listing(self, seller_id: str, sku: str) -> dict:
        api = self._listings_api()
        return await self._call(
            api.get_listings_item,
            sellerId=seller_id, sku=sku,
            marketplaceIds=[self._marketplace_id],
        )

    async def delete_listing(self, seller_id: str, sku: str) -> dict:
        api = self._listings_api()
        return await self._call(
            api.delete_listings_item,
            sellerId=seller_id, sku=sku,
            marketplaceIds=[self._marketplace_id],
        )

    # --- Listing Restrictions ---

    async def get_listing_restrictions(
        self, asin: str, condition_type: str = "used_very_good"
    ) -> list[dict]:
        """Check listing restrictions for an ASIN.

        Returns a list of restriction objects. Empty list = no restrictions (listable).
        Each restriction has: conditionType, reasons[{reasonCode, message, links}].
        """
        api = self._restrictions_api()
        try:
            result = await self._call(
                api.get_listings_restrictions,
                asin=asin,
                sellerId=settings.sp_api_seller_id,
                marketplaceIds=[self._marketplace_id],
                conditionType=condition_type,
                reasonLocale="ja_JP",
            )
        except AmazonApiError:
            # If the restrictions API itself fails, return empty (allow listing attempt)
            logger.warning("Listing restrictions check failed for ASIN %s", asin)
            return []

        if isinstance(result, dict):
            return result.get("restrictions", [])
        return []
