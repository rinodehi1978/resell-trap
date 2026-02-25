"""Async wrapper around python-amazon-sp-api (synchronous library)."""

from __future__ import annotations

import asyncio
import logging
import time
from functools import partial
from typing import Any

from sp_api.api import CatalogItems, Feeds, ListingsItems, ListingsRestrictions, Orders, ProductFees
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
        self._fee_cache: dict[str, float] = {}  # ASIN → referral fee %
        self._fee_cache_max: int = 200
        self._last_fee_request_at: float = 0.0

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

    def _fees_api(self) -> ProductFees:
        return ProductFees(
            credentials=self._credentials,
            marketplace=self._marketplace,
        )

    def _feeds_api(self) -> Feeds:
        return Feeds(
            credentials=self._credentials,
            marketplace=self._marketplace,
        )

    def _orders_api(self) -> Orders:
        return Orders(
            credentials=self._credentials,
            marketplace=self._marketplace,
        )

    # --- Orders ---

    async def get_order_items(self, order_id: str) -> list[dict]:
        """Get line items for a specific order (includes SKU, ASIN, title, price)."""
        api = self._orders_api()
        result = await self._call(api.get_order_items, order_id)
        return result.get("OrderItems", []) if isinstance(result, dict) else []

    async def get_new_orders(self, created_after: str) -> list[dict]:
        """Get orders created after the given ISO timestamp.

        Returns a list of order dicts with OrderStatus='Unshipped'.
        """
        api = self._orders_api()
        result = await self._call(
            api.get_orders,
            CreatedAfter=created_after,
            MarketplaceIds=[self._marketplace_id],
            OrderStatuses=["Unshipped"],
        )
        return result.get("Orders", []) if isinstance(result, dict) else []

    # --- Catalog ---

    async def get_catalog_item(self, asin: str) -> dict:
        api = self._catalog_api()
        return await self._call(
            api.get_catalog_item,
            asin=asin,
            marketplaceIds=[self._marketplace_id],
            includedData=["summaries", "images", "salesRanks"],
        )

    async def get_product_type(self, asin: str) -> str:
        """Get the Amazon product type for an ASIN (e.g. 'SPACE_HEATER').

        Falls back to 'PRODUCT' if lookup fails.
        """
        try:
            api = self._catalog_api()
            result = await self._call(
                api.get_catalog_item,
                asin=asin,
                marketplaceIds=[self._marketplace_id],
                includedData=["productTypes"],
            )
            product_types = result.get("productTypes", []) if isinstance(result, dict) else []
            for pt in product_types:
                if pt.get("productType"):
                    logger.debug("ASIN %s productType: %s", asin, pt["productType"])
                    return pt["productType"]
        except AmazonApiError as e:
            logger.warning("Failed to get productType for ASIN %s: %s", asin, e)
        return "PRODUCT"

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
        self, seller_id: str, sku: str, product_type: str, attributes: dict,
        *, offer_only: bool = False,
    ) -> dict:
        api = self._listings_api()
        body = {"productType": product_type, "attributes": attributes}
        if offer_only:
            body["requirements"] = "LISTING_OFFER_ONLY"
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None,
                partial(
                    api.put_listings_item,
                    sellerId=seller_id,
                    sku=sku,
                    marketplaceIds=[self._marketplace_id],
                    body=body,
                ),
            )
        except SellingApiException as e:
            raise AmazonApiError(str(e), getattr(e, "status_code", None)) from e

        payload = result.payload if hasattr(result, "payload") else result or {}
        status = payload.get("status", "")
        if status == "INVALID":
            issues = payload.get("issues", [])
            msgs = "; ".join(i.get("message", i.get("code", "")) for i in issues)
            raise AmazonApiError(
                f"Listing rejected (INVALID): {msgs or 'unknown error'}",
            )

        logger.info(
            "Listing created: SKU=%s status=%s submissionId=%s",
            sku, status, payload.get("submissionId", ""),
        )
        return payload


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

    async def patch_offer_images(
        self, seller_id: str, sku: str, image_urls: list[str],
    ) -> dict:
        """PATCH offer-level images onto an existing listing."""
        api = self._listings_api()
        patches = []
        if image_urls:
            patches.append({
                "op": "replace",
                "path": "/attributes/main_offer_image_locator",
                "value": [{"media_location": image_urls[0]}],
            })
        for i, url in enumerate(image_urls[1:6]):
            patches.append({
                "op": "replace",
                "path": f"/attributes/other_offer_image_locator_{i + 1}",
                "value": [{"media_location": url}],
            })
        if not patches:
            return {}
        body = {"productType": "PRODUCT", "patches": patches}
        return await self._call(
            api.patch_listings_item,
            sellerId=seller_id, sku=sku,
            marketplaceIds=[self._marketplace_id], body=body,
        )

    # --- Feeds API (price & inventory sync to Seller Central) ---
    # XML feeds (POST_PRODUCT_PRICING_DATA等) は403で使用不可。
    # JSON_LISTINGS_FEED を使用してセラーセントラルに反映する。

    async def submit_price_feed(self, seller_id: str, sku: str, price_jpy: int) -> dict:
        """Submit a JSON_LISTINGS_FEED to update price in Seller Central."""
        import json
        from io import BytesIO

        feed_data = {
            "header": {
                "sellerId": seller_id,
                "version": "2.0",
                "issueLocale": "ja_JP",
            },
            "messages": [{
                "messageId": 1,
                "sku": sku,
                "operationType": "PATCH",
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
            }],
        }
        return await self._submit_json_feed(feed_data)

    async def submit_inventory_feed(self, seller_id: str, sku: str, quantity: int, lead_time: int = 4) -> dict:
        """Submit a JSON_LISTINGS_FEED to update inventory in Seller Central."""
        import json
        from io import BytesIO

        feed_data = {
            "header": {
                "sellerId": seller_id,
                "version": "2.0",
                "issueLocale": "ja_JP",
            },
            "messages": [{
                "messageId": 1,
                "sku": sku,
                "operationType": "PATCH",
                "productType": "PRODUCT",
                "patches": [{
                    "op": "replace",
                    "path": "/attributes/fulfillment_availability",
                    "value": [{
                        "fulfillment_channel_code": "DEFAULT",
                        "quantity": quantity,
                    }],
                }],
            }],
        }
        return await self._submit_json_feed(feed_data)

    async def _submit_json_feed(self, feed_data: dict) -> dict:
        """Submit a JSON_LISTINGS_FEED via Feeds API."""
        import json
        from io import BytesIO

        body = json.dumps(feed_data, ensure_ascii=False).encode("utf-8")
        api = self._feeds_api()
        loop = asyncio.get_event_loop()
        try:
            doc_response, feed_response = await loop.run_in_executor(
                None,
                partial(
                    api.submit_feed,
                    "JSON_LISTINGS_FEED",
                    BytesIO(body),
                    content_type="application/json; charset=UTF-8",
                    marketplaceIds=[self._marketplace_id],
                ),
            )
            return feed_response.payload
        except SellingApiException as e:
            raise AmazonApiError(str(e), getattr(e, "status_code", None)) from e

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

    # --- Product Fees ---

    async def get_referral_fee_pct(self, asin: str, price: int) -> float | None:
        """Get Amazon referral fee percentage for an ASIN.

        Uses an in-memory cache (fee % is category-dependent and stable).
        Rate-limited to 1 request/second per SP-API constraints.

        Returns the fee percentage (e.g. 15.0 for 15%), or None on error.
        """
        if asin in self._fee_cache:
            return self._fee_cache[asin]

        if price <= 0:
            return None

        # Rate limiting: 1 req/sec
        now = time.monotonic()
        elapsed = now - self._last_fee_request_at
        if elapsed < 1.0:
            await asyncio.sleep(1.0 - elapsed)

        try:
            api = self._fees_api()
            self._last_fee_request_at = time.monotonic()
            result = await self._call(
                api.get_product_fees_estimate_for_asin,
                asin=asin,
                price=float(price),
                currency="JPY",
                is_fba=False,
            )
        except AmazonApiError as e:
            logger.warning("Fee estimate failed for ASIN %s: %s", asin, e)
            return None

        # Extract referral fee from response
        try:
            fees_estimate = result.get("FeesEstimateResult", {}).get("FeesEstimate", {})
            for fee in fees_estimate.get("FeeDetailList", []):
                if fee.get("FeeType") == "ReferralFee":
                    fee_amount = float(fee["FeeAmount"]["Amount"])
                    fee_pct = round(fee_amount / price * 100, 1)
                    if len(self._fee_cache) >= self._fee_cache_max:
                        self._fee_cache.clear()
                    self._fee_cache[asin] = fee_pct
                    logger.debug(
                        "ASIN %s referral fee: %.1f%% (¥%d on ¥%d)",
                        asin, fee_pct, fee_amount, price,
                    )
                    return fee_pct
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("Failed to parse fee response for ASIN %s: %s", asin, e)

        return None
