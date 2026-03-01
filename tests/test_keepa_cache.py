"""Tests for Keepa search result caching."""

from time import monotonic
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from yafuama.keepa.client import KeepaClient


@pytest.fixture()
def keepa_client():
    """Create a KeepaClient with mocked HTTP client."""
    with patch.object(KeepaClient, "__init__", lambda self: None):
        client = KeepaClient.__new__(KeepaClient)
        client._api_key = "test_key"
        client._client = AsyncMock()
        client._tokens_left = 100
        client._search_cache = {}
        return client


def _mock_response(products):
    """Create a mock HTTP response with synchronous .json()."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"products": products, "tokensLeft": 99}
    return resp


class TestSearchCache:
    @pytest.mark.asyncio
    async def test_cache_stores_results(self, keepa_client):
        products = [{"asin": "B001", "title": "Product A"}]
        keepa_client._client.get = AsyncMock(return_value=_mock_response(products))

        result1 = await keepa_client.search_products("test query", stats=90)
        assert result1 == products
        assert keepa_client._client.get.call_count == 1

        # Second call should hit cache
        result2 = await keepa_client.search_products("test query", stats=90)
        assert result2 == products
        assert keepa_client._client.get.call_count == 1  # No additional HTTP call

    @pytest.mark.asyncio
    async def test_different_queries_not_cached(self, keepa_client):
        products_a = [{"asin": "B001", "title": "A"}]
        products_b = [{"asin": "B002", "title": "B"}]
        keepa_client._client.get = AsyncMock(
            side_effect=[_mock_response(products_a), _mock_response(products_b)]
        )

        result1 = await keepa_client.search_products("query A", stats=90)
        result2 = await keepa_client.search_products("query B", stats=90)
        assert result1 == products_a
        assert result2 == products_b
        assert keepa_client._client.get.call_count == 2

    @pytest.mark.asyncio
    async def test_different_stats_not_cached(self, keepa_client):
        products = [{"asin": "B001", "title": "A"}]
        keepa_client._client.get = AsyncMock(return_value=_mock_response(products))

        await keepa_client.search_products("same query", stats=30)
        await keepa_client.search_products("same query", stats=90)
        assert keepa_client._client.get.call_count == 2

    def test_clear_search_cache_evicts_expired(self, keepa_client):
        now = monotonic()
        # Old entry (expired) and fresh entry
        keepa_client._search_cache = {
            "old": (now - 9999, [{"asin": "B001"}]),
            "fresh": (now, [{"asin": "B002"}]),
        }
        keepa_client.clear_search_cache()
        assert "old" not in keepa_client._search_cache
        assert "fresh" in keepa_client._search_cache

    @pytest.mark.asyncio
    async def test_expired_cache_allows_fresh_fetch(self, keepa_client):
        products = [{"asin": "B001", "title": "A"}]
        keepa_client._client.get = AsyncMock(return_value=_mock_response(products))

        await keepa_client.search_products("query", stats=90)
        assert keepa_client._client.get.call_count == 1

        # Expire the cache entry
        for key in keepa_client._search_cache:
            ts, data = keepa_client._search_cache[key]
            keepa_client._search_cache[key] = (ts - 9999, data)

        await keepa_client.search_products("query", stats=90)
        assert keepa_client._client.get.call_count == 2
