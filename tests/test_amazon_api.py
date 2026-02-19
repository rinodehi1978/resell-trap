"""Tests for Amazon API endpoints."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from resell_trap.amazon import AmazonApiError
from resell_trap.database import Base, get_db
from resell_trap.main import app, app_state
from resell_trap.schemas import AuctionData


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


class TestSpApiNotConfigured:
    def test_503_when_not_configured(self, test_db, mock_scraper):
        """Without sp_api in app_state, Amazon endpoints return 503."""
        app_state.pop("sp_api", None)
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.get("/api/amazon/catalog/search?keywords=test")
        assert resp.status_code == 503
        app_state.clear()
