"""Async Keepa API client using httpx (no heavy dependencies)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..config import settings
from . import KeepaApiError

logger = logging.getLogger(__name__)

KEEPA_API_BASE = "https://api.keepa.com"
DOMAIN_JP = 5  # Amazon.co.jp

# Keepa epoch: 2011-01-01 00:00 UTC (times stored as minutes since)
_KEEPA_EPOCH = datetime(2011, 1, 1, tzinfo=timezone.utc)


def keepa_minutes_to_datetime(minutes: int) -> datetime:
    """Convert Keepa time (minutes since 2011-01-01) to UTC datetime."""
    return _KEEPA_EPOCH + timedelta(minutes=minutes)


_SEARCH_CACHE_MAX = 50  # Max cached search results to prevent OOM


class KeepaClient:
    """Async Keepa API client for Amazon.co.jp product data."""

    def __init__(self) -> None:
        self._api_key = settings.keepa_api_key
        self._client = httpx.AsyncClient(timeout=30.0)
        self._tokens_left: int | None = None
        self._search_cache: dict[str, list[dict[str, Any]]] = {}

    async def query_product(
        self,
        asin: str,
        stats: int | None = None,
        history: bool = False,
    ) -> dict[str, Any]:
        """Fetch product data for a single ASIN.

        Args:
            asin: Amazon ASIN to look up.
            stats: Days for pre-computed statistics (default from settings).
            history: Include full price/rank CSV history arrays.

        Returns:
            Product data dict from Keepa API.
        """
        products = await self._request(
            asins=[asin],
            stats=stats or settings.keepa_default_stats_days,
            history=history,
        )
        if not products:
            raise KeepaApiError(f"No product data returned for ASIN {asin}")
        return products[0]

    async def query_products(
        self,
        asins: list[str],
        stats: int | None = None,
        history: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch product data for multiple ASINs (max 100 per request)."""
        return await self._request(
            asins=asins,
            stats=stats or settings.keepa_default_stats_days,
            history=history,
        )

    async def search_products(
        self,
        term: str,
        stats: int | None = None,
    ) -> list[dict[str, Any]]:
        """Search Keepa product database by keyword.

        Returns up to 40 product dicts with stats.
        Each result costs 1 token.
        Results are cached in-memory per scan cycle.
        """
        stat_days = stats or settings.keepa_default_stats_days
        cache_key = f"{term}:{stat_days}"
        if cache_key in self._search_cache:
            logger.debug("Keepa search cache hit: %s", term)
            return self._search_cache[cache_key]

        params = {
            "key": self._api_key,
            "domain": DOMAIN_JP,
            "type": "product",
            "term": term,
            "stats": stat_days,
        }

        try:
            resp = await self._client.get(f"{KEEPA_API_BASE}/search", params=params)
        except httpx.HTTPError as e:
            raise KeepaApiError(f"Keepa HTTP error: {e}") from e

        if resp.status_code != 200:
            raise KeepaApiError(
                f"Keepa API returned {resp.status_code}: {resp.text}",
                tokens_left=self._tokens_left,
            )

        data = resp.json()
        self._tokens_left = data.get("tokensLeft")

        if self._tokens_left is not None and self._tokens_left <= 0:
            logger.warning("Keepa API tokens exhausted (tokensLeft=%s)", self._tokens_left)

        products = data.get("products")
        if products is None:
            error_msg = data.get("error", "Unknown error")
            raise KeepaApiError(
                f"Keepa search error: {error_msg}",
                tokens_left=self._tokens_left,
            )

        if len(self._search_cache) >= _SEARCH_CACHE_MAX:
            self._search_cache.clear()
        self._search_cache[cache_key] = products
        return products

    async def product_finder(
        self,
        selection: dict[str, Any],
        stats: int | None = None,
    ) -> list[dict[str, Any]]:
        """Keepa Product Finder: search products by filter criteria.

        Uses the /query endpoint to get ASIN list, then fetches product details.
        Token cost: ~0.5 per ASIN in result + 1 per product detail.

        Args:
            selection: Product Finder filter criteria (e.g. salesRankDrops30_gte, current_USED_gte).
            stats: Days for pre-computed statistics.

        Returns:
            List of product data dicts with full details.
        """
        params = {
            "key": self._api_key,
            "domain": DOMAIN_JP,
            "selection": json.dumps(selection),
        }

        try:
            resp = await self._client.get(f"{KEEPA_API_BASE}/query", params=params)
        except httpx.HTTPError as e:
            raise KeepaApiError(f"Keepa HTTP error: {e}") from e

        if resp.status_code != 200:
            raise KeepaApiError(
                f"Keepa API returned {resp.status_code}: {resp.text}",
                tokens_left=self._tokens_left,
            )

        data = resp.json()
        self._tokens_left = data.get("tokensLeft")

        if self._tokens_left is not None and self._tokens_left <= 0:
            logger.warning("Keepa API tokens exhausted (tokensLeft=%s)", self._tokens_left)

        asin_list = data.get("asinList", [])
        if not asin_list:
            logger.info("Product Finder returned 0 ASINs")
            return []

        logger.info("Product Finder returned %d ASINs", len(asin_list))

        # Fetch product details for the ASINs (max 100)
        stat_days = stats or settings.keepa_default_stats_days
        products = await self.query_products(asin_list[:50], stats=stat_days)
        return products

    def clear_search_cache(self) -> None:
        """Clear the in-memory search result cache. Call at the start of each scan cycle."""
        self._search_cache.clear()

    @property
    def tokens_left(self) -> int | None:
        """Remaining API tokens (updated after each request)."""
        return self._tokens_left

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def _request(
        self,
        asins: list[str],
        stats: int,
        history: bool,
    ) -> list[dict[str, Any]]:
        """Execute a product query against the Keepa API."""
        params = {
            "key": self._api_key,
            "domain": DOMAIN_JP,
            "asin": ",".join(asins),
            "stats": stats,
            "history": 1 if history else 0,
        }

        try:
            resp = await self._client.get(f"{KEEPA_API_BASE}/product", params=params)
        except httpx.HTTPError as e:
            raise KeepaApiError(f"Keepa HTTP error: {e}") from e

        if resp.status_code != 200:
            raise KeepaApiError(
                f"Keepa API returned {resp.status_code}: {resp.text}",
                tokens_left=self._tokens_left,
            )

        data = resp.json()
        self._tokens_left = data.get("tokensLeft")

        if self._tokens_left is not None and self._tokens_left <= 0:
            logger.warning("Keepa API tokens exhausted (tokensLeft=%s)", self._tokens_left)

        products = data.get("products")
        if products is None:
            error_msg = data.get("error", "Unknown error")
            raise KeepaApiError(
                f"Keepa API error: {error_msg}",
                tokens_left=self._tokens_left,
            )

        return products
