"""Tests for Amazon API endpoints."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from yafuama.amazon import AmazonApiError
from yafuama.database import Base, get_db
from yafuama.main import app, app_state
from yafuama.schemas import AuctionData


@pytest.fixture()
def test_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestSession
    app.dependency_overrides.clear()


@pytest.fixture()
def mock_scraper():
    scraper = AsyncMock()
    app_state["scraper"] = scraper
    app_state["scheduler"] = None
    return scraper


@pytest.fixture()
def mock_sp_client():
    client = AsyncMock()
    client.search_catalog_items = AsyncMock(return_value=[])
    client.get_catalog_item = AsyncMock(return_value={})
    client.create_listing = AsyncMock(return_value={"status": "ACCEPTED"})
    client.patch_listing_quantity = AsyncMock(return_value={})
    client.patch_listing_price = AsyncMock(return_value={})
    client.delete_listing = AsyncMock(return_value={})
    client.get_listing_restrictions = AsyncMock(return_value=[])  # No restrictions by default
    app_state["sp_api"] = client
    return client


@pytest.fixture()
def client(test_db, mock_scraper, mock_sp_client):
    c = TestClient(app, raise_server_exceptions=False)
    yield c
    app_state.clear()


MOCK_AUCTION = AuctionData(
    auction_id="1219987808",
    title="Test Pokemon Cards",
    url="https://auctions.yahoo.co.jp/jp/auction/1219987808",
    image_url="https://example.com/img.jpg",
    category_id="2084309054",
    seller_id="seller123",
    current_price=3600,
    start_price=1111,
    buy_now_price=0,
    win_price=0,
    bid_count=5,
    is_closed=False,
    has_winner=False,
)


def _create_monitored_item(client, mock_scraper):
    """Helper: create a monitored item via the items API."""
    mock_scraper.fetch_auction = AsyncMock(return_value=MOCK_AUCTION)
    resp = client.post("/api/items", json={"auction_id": "1219987808"})
    assert resp.status_code == 201
    return resp.json()


class TestCatalogSearch:
    def test_search_empty(self, client, mock_sp_client):
        resp = client.get("/api/amazon/catalog/search?keywords=pokemon")
        assert resp.status_code == 200
        data = resp.json()
        assert data["keywords"] == "pokemon"
        assert data["items"] == []

    def test_search_with_results(self, client, mock_sp_client):
        mock_sp_client.search_catalog_items = AsyncMock(return_value=[
            {
                "asin": "B08XYZ",
                "summaries": [{"itemName": "Pokemon Card", "brand": "Nintendo"}],
                "images": [{"images": [{"link": "https://img.example.com/1.jpg"}]}],
            }
        ])
        resp = client.get("/api/amazon/catalog/search?keywords=pokemon")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["asin"] == "B08XYZ"
        assert items[0]["title"] == "Pokemon Card"

    def test_search_sp_api_error(self, client, mock_sp_client):
        mock_sp_client.search_catalog_items.side_effect = AmazonApiError("throttle", 429)
        resp = client.get("/api/amazon/catalog/search?keywords=pokemon")
        assert resp.status_code == 502


class TestCatalogItem:
    def test_get_catalog_item(self, client, mock_sp_client):
        mock_sp_client.get_catalog_item = AsyncMock(return_value={"asin": "B08XYZ"})
        resp = client.get("/api/amazon/catalog/B08XYZ")
        assert resp.status_code == 200


class TestCreateListing:
    def test_create_listing(self, client, mock_scraper, mock_sp_client):
        _create_monitored_item(client, mock_scraper)
        resp = client.post("/api/amazon/listings", json={
            "auction_id": "1219987808",
            "asin": "B08XYZ",
            "estimated_win_price": 3000,
            "shipping_cost": 800,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["amazon_sku"] == "YAHOO-1219987808"
        assert data["amazon_listing_status"] == "active"
        assert data["amazon_price"] > 0
        mock_sp_client.create_listing.assert_called_once()

    def test_create_listing_custom_sku(self, client, mock_scraper, mock_sp_client):
        _create_monitored_item(client, mock_scraper)
        resp = client.post("/api/amazon/listings", json={
            "auction_id": "1219987808",
            "sku": "CUSTOM-SKU-01",
            "estimated_win_price": 5000,
        })
        assert resp.status_code == 201
        assert resp.json()["amazon_sku"] == "CUSTOM-SKU-01"

    def test_create_listing_duplicate(self, client, mock_scraper, mock_sp_client):
        _create_monitored_item(client, mock_scraper)
        client.post("/api/amazon/listings", json={
            "auction_id": "1219987808",
            "estimated_win_price": 3000,
        })
        resp = client.post("/api/amazon/listings", json={
            "auction_id": "1219987808",
            "estimated_win_price": 3000,
        })
        assert resp.status_code == 409

    def test_create_listing_item_not_found(self, client, mock_sp_client):
        resp = client.post("/api/amazon/listings", json={
            "auction_id": "nonexistent",
            "estimated_win_price": 3000,
        })
        assert resp.status_code == 404

    def test_create_listing_sp_api_error(self, client, mock_scraper, mock_sp_client):
        _create_monitored_item(client, mock_scraper)
        mock_sp_client.create_listing.side_effect = AmazonApiError("error", 500)
        resp = client.post("/api/amazon/listings", json={
            "auction_id": "1219987808",
            "estimated_win_price": 3000,
        })
        assert resp.status_code == 502


class TestGetListing:
    def test_get_listing(self, client, mock_scraper, mock_sp_client):
        _create_monitored_item(client, mock_scraper)
        client.post("/api/amazon/listings", json={
            "auction_id": "1219987808",
            "estimated_win_price": 3000,
        })
        resp = client.get("/api/amazon/listings/1219987808")
        assert resp.status_code == 200
        assert resp.json()["amazon_sku"] == "YAHOO-1219987808"

    def test_get_listing_no_sku(self, client, mock_scraper, mock_sp_client):
        _create_monitored_item(client, mock_scraper)
        resp = client.get("/api/amazon/listings/1219987808")
        assert resp.status_code == 404


class TestUpdateListing:
    def test_update_price(self, client, mock_scraper, mock_sp_client):
        _create_monitored_item(client, mock_scraper)
        client.post("/api/amazon/listings", json={
            "auction_id": "1219987808",
            "estimated_win_price": 3000,
            "shipping_cost": 800,
        })
        resp = client.patch("/api/amazon/listings/1219987808", json={
            "estimated_win_price": 4000,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["estimated_win_price"] == 4000
        assert data["amazon_price"] > 0

    def test_update_explicit_price(self, client, mock_scraper, mock_sp_client):
        _create_monitored_item(client, mock_scraper)
        client.post("/api/amazon/listings", json={
            "auction_id": "1219987808",
            "estimated_win_price": 3000,
        })
        resp = client.patch("/api/amazon/listings/1219987808", json={
            "amazon_price": 9999,
        })
        assert resp.status_code == 200
        assert resp.json()["amazon_price"] == 9999


class TestDeleteListing:
    def test_delete_listing(self, client, mock_scraper, mock_sp_client):
        _create_monitored_item(client, mock_scraper)
        client.post("/api/amazon/listings", json={
            "auction_id": "1219987808",
            "estimated_win_price": 3000,
        })
        resp = client.delete("/api/amazon/listings/1219987808")
        assert resp.status_code == 204
        mock_sp_client.delete_listing.assert_called_once()

        # Listing should be gone
        resp = client.get("/api/amazon/listings/1219987808")
        assert resp.status_code == 404


class TestSyncListing:
    def test_sync_active(self, client, mock_scraper, mock_sp_client):
        _create_monitored_item(client, mock_scraper)
        client.post("/api/amazon/listings", json={
            "auction_id": "1219987808",
            "estimated_win_price": 3000,
        })
        resp = client.post("/api/amazon/listings/1219987808/sync")
        assert resp.status_code == 200
        assert resp.json()["amazon_listing_status"] == "active"
        mock_sp_client.patch_listing_quantity.assert_called_once()


class TestListingRestrictions:
    def test_no_restrictions(self, client, mock_sp_client):
        """ASIN with no restrictions → is_listable=True."""
        mock_sp_client.get_listing_restrictions = AsyncMock(return_value=[])
        resp = client.get("/api/amazon/restrictions/B08XYZ")
        assert resp.status_code == 200
        data = resp.json()
        assert data["asin"] == "B08XYZ"
        assert data["is_listable"] is True
        assert data["restrictions"] == []

    def test_brand_gated(self, client, mock_sp_client):
        """ASIN with brand restriction → is_listable=False with reasons."""
        mock_sp_client.get_listing_restrictions = AsyncMock(return_value=[
            {
                "conditionType": "used_very_good",
                "reasons": [{
                    "reasonCode": "APPROVAL_REQUIRED",
                    "message": "このブランドの出品には申請が必要です",
                    "links": [{"resource": "https://sellercentral.amazon.co.jp/..."}],
                }],
            }
        ])
        resp = client.get("/api/amazon/restrictions/B08XYZ")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_listable"] is False
        assert len(data["restrictions"]) == 1
        assert data["restrictions"][0]["is_restricted"] is True
        assert "申請" in data["restrictions"][0]["reasons"][0]["message"]

    def test_create_listing_blocked_by_restriction(self, client, mock_scraper, mock_sp_client):
        """create_listing returns 403 when ASIN is brand-gated."""
        _create_monitored_item(client, mock_scraper)
        mock_sp_client.get_listing_restrictions = AsyncMock(return_value=[
            {
                "conditionType": "used_very_good",
                "reasons": [{"reasonCode": "APPROVAL_REQUIRED", "message": "Brand gated"}],
            }
        ])
        resp = client.post("/api/amazon/listings", json={
            "auction_id": "1219987808",
            "asin": "B08XYZ",
            "estimated_win_price": 3000,
        })
        assert resp.status_code == 403
        assert "restricted" in resp.json()["detail"].lower()
        # Should NOT have called create_listing on SP-API
        mock_sp_client.create_listing.assert_not_called()

    def test_create_listing_allowed_when_no_restriction(self, client, mock_scraper, mock_sp_client):
        """create_listing succeeds when ASIN has no restrictions."""
        _create_monitored_item(client, mock_scraper)
        mock_sp_client.get_listing_restrictions = AsyncMock(return_value=[])
        resp = client.post("/api/amazon/listings", json={
            "auction_id": "1219987808",
            "asin": "B08XYZ",
            "estimated_win_price": 3000,
            "shipping_cost": 800,
        })
        assert resp.status_code == 201
        mock_sp_client.create_listing.assert_called_once()

    def test_custom_condition_param(self, client, mock_sp_client):
        """Restriction check with specific condition type."""
        mock_sp_client.get_listing_restrictions = AsyncMock(return_value=[])
        resp = client.get("/api/amazon/restrictions/B08XYZ?condition=used_good")
        assert resp.status_code == 200
        assert resp.json()["is_listable"] is True


class TestShippingPatterns:
    def test_list_shipping_patterns(self, client):
        resp = client.get("/api/amazon/shipping-patterns")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["patterns"]) == 3
        keys = [p["key"] for p in data["patterns"]]
        assert keys == ["1_2_days", "2_3_days", "3_7_days"]
        assert data["patterns"][0]["lead_time_days"] == 4
        assert data["patterns"][1]["lead_time_days"] == 6
        assert data["patterns"][2]["lead_time_days"] == 9
        assert len(data["delivery_regions"]) == 6

    def test_create_listing_with_shipping_pattern(self, client, mock_scraper, mock_sp_client):
        """Shipping pattern sets correct lead_time and merchant_shipping_group."""
        _create_monitored_item(client, mock_scraper)
        resp = client.post("/api/amazon/listings", json={
            "auction_id": "1219987808",
            "asin": "B08XYZ",
            "estimated_win_price": 3000,
            "shipping_cost": 800,
            "shipping_pattern": "1_2_days",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["amazon_lead_time_days"] == 4
        assert data["amazon_shipping_pattern"] == "1_2_days"
        # Verify SP-API call includes merchant_shipping_group
        call_kwargs = mock_sp_client.create_listing.call_args
        attributes = call_kwargs[1]["attributes"] if "attributes" in call_kwargs[1] else call_kwargs[0][3]
        assert "merchant_shipping_group" in attributes

    def test_create_listing_with_3_7_days_pattern(self, client, mock_scraper, mock_sp_client):
        """3〜7日パターン → リードタイム9日."""
        _create_monitored_item(client, mock_scraper)
        resp = client.post("/api/amazon/listings", json={
            "auction_id": "1219987808",
            "asin": "B08XYZ",
            "estimated_win_price": 3000,
            "shipping_cost": 800,
            "shipping_pattern": "3_7_days",
        })
        assert resp.status_code == 201
        assert resp.json()["amazon_lead_time_days"] == 9
        assert resp.json()["amazon_shipping_pattern"] == "3_7_days"

    def test_create_listing_default_pattern(self, client, mock_scraper, mock_sp_client):
        """Default shipping_pattern is 2_3_days."""
        _create_monitored_item(client, mock_scraper)
        resp = client.post("/api/amazon/listings", json={
            "auction_id": "1219987808",
            "asin": "B08XYZ",
            "estimated_win_price": 3000,
            "shipping_cost": 800,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["amazon_lead_time_days"] == 6
        assert data["amazon_shipping_pattern"] == "2_3_days"

    def test_invalid_shipping_pattern(self, client, mock_scraper, mock_sp_client):
        """Invalid shipping pattern returns 400."""
        _create_monitored_item(client, mock_scraper)
        resp = client.post("/api/amazon/listings", json={
            "auction_id": "1219987808",
            "asin": "B08XYZ",
            "estimated_win_price": 3000,
            "shipping_pattern": "invalid_pattern",
        })
        assert resp.status_code == 400
        assert "shipping_pattern" in resp.json()["detail"].lower()


class TestSpApiNotConfigured:
    def test_503_when_not_configured(self, test_db, mock_scraper):
        """Without sp_api in app_state, Amazon endpoints return 503."""
        app_state.pop("sp_api", None)
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.get("/api/amazon/catalog/search?keywords=test")
        assert resp.status_code == 503
        app_state.clear()
