"""Tests for AmazonNotifier."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yafuama.amazon import AmazonApiError
from yafuama.amazon.notifier import AmazonNotifier


def _make_item(**kwargs):
    defaults = dict(
        id=1,
        auction_id="1219987808",
        title="Test Item",
        url="https://example.com",
        image_url="",
        category_id="",
        seller_id="seller",
        current_price=3000,
        start_price=1000,
        buy_now_price=0,
        win_price=0,
        bid_count=5,
        status="active",
        amazon_sku="YAHOO-1219987808",
        amazon_listing_status="active",
        amazon_last_synced_at=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_change(**kwargs):
    defaults = dict(
        change_type="status_change",
        old_status="active",
        new_status="ended_sold",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.fixture()
def mock_client():
    client = AsyncMock()
    client.patch_listing_quantity = AsyncMock(return_value={})
    return client


@pytest.fixture()
def notifier(mock_client):
    return AmazonNotifier(mock_client, seller_id="TEST_SELLER")


class TestAmazonNotifier:
    @pytest.mark.asyncio
    async def test_ended_sets_quantity_zero(self, notifier, mock_client):
        item = _make_item()
        change = _make_change(new_status="ended_sold")
        result = await notifier.notify(item, change)
        assert result is True
        mock_client.patch_listing_quantity.assert_called_once_with("TEST_SELLER", "YAHOO-1219987808", 0)
        assert item.amazon_listing_status == "inactive"

    @pytest.mark.asyncio
    async def test_ended_no_winner(self, notifier, mock_client):
        item = _make_item()
        change = _make_change(new_status="ended_no_winner")
        result = await notifier.notify(item, change)
        assert result is True
        mock_client.patch_listing_quantity.assert_called_once_with("TEST_SELLER", "YAHOO-1219987808", 0)

    @pytest.mark.asyncio
    async def test_active_sets_quantity_one(self, notifier, mock_client):
        item = _make_item(amazon_listing_status="inactive")
        change = _make_change(old_status="ended_no_winner", new_status="active")
        result = await notifier.notify(item, change)
        assert result is True
        mock_client.patch_listing_quantity.assert_called_once_with("TEST_SELLER", "YAHOO-1219987808", 1)
        assert item.amazon_listing_status == "active"

    @pytest.mark.asyncio
    async def test_skip_no_sku(self, notifier, mock_client):
        item = _make_item(amazon_sku=None)
        change = _make_change(new_status="ended_sold")
        result = await notifier.notify(item, change)
        assert result is True
        mock_client.patch_listing_quantity.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_non_status_change(self, notifier, mock_client):
        item = _make_item()
        change = _make_change(change_type="price_change")
        result = await notifier.notify(item, change)
        assert result is True
        mock_client.patch_listing_quantity.assert_not_called()

    @pytest.mark.asyncio
    async def test_api_error_returns_false(self, notifier, mock_client):
        mock_client.patch_listing_quantity.side_effect = AmazonApiError("rate limit", 429)
        item = _make_item()
        change = _make_change(new_status="ended_sold")
        result = await notifier.notify(item, change)
        assert result is False
        assert item.amazon_listing_status == "error"

    @pytest.mark.asyncio
    async def test_last_synced_updated(self, notifier, mock_client):
        item = _make_item(amazon_last_synced_at=None)
        change = _make_change(new_status="ended_sold")
        before = datetime.now(timezone.utc)
        await notifier.notify(item, change)
        assert item.amazon_last_synced_at is not None
        assert item.amazon_last_synced_at >= before

    def test_format_message_with_sku(self, notifier):
        item = _make_item()
        change = _make_change()
        msg = notifier.format_message(item, change)
        assert "YAHOO-1219987808" in msg
        assert "Amazon SKU" in msg

    def test_format_message_without_sku(self, notifier):
        item = _make_item(amazon_sku=None)
        change = _make_change()
        msg = notifier.format_message(item, change)
        assert "Amazon SKU" not in msg
