"""Tests for API endpoints using FastAPI TestClient."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

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
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def mock_scraper():
    scraper = AsyncMock()
    app_state["scraper"] = scraper
    app_state["scheduler"] = None
    yield scraper
    app_state.clear()


@pytest.fixture()
def client(test_db, mock_scraper):
    return TestClient(app, raise_server_exceptions=False)


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


class TestHealthEndpoint:
    def test_health(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "degraded")
        assert "services" in data
        assert isinstance(data["services"], list)


class TestItemsCRUD:
    def test_create_item(self, client, mock_scraper):
        mock_scraper.fetch_auction = AsyncMock(return_value=MOCK_AUCTION)
        resp = client.post("/api/items", json={"auction_id": "1219987808"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["auction_id"] == "1219987808"
        assert data["current_price"] == 3600
        assert data["status"] == "active"

    def test_create_duplicate(self, client, mock_scraper):
        mock_scraper.fetch_auction = AsyncMock(return_value=MOCK_AUCTION)
        client.post("/api/items", json={"auction_id": "1219987808"})
        resp = client.post("/api/items", json={"auction_id": "1219987808"})
        assert resp.status_code == 409

    def test_create_bad_id(self, client):
        resp = client.post("/api/items", json={"auction_id": "bad"})
        assert resp.status_code == 400

    def test_list_items(self, client, mock_scraper):
        mock_scraper.fetch_auction = AsyncMock(return_value=MOCK_AUCTION)
        client.post("/api/items", json={"auction_id": "1219987808"})
        resp = client.get("/api/items")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1

    def test_get_item(self, client, mock_scraper):
        mock_scraper.fetch_auction = AsyncMock(return_value=MOCK_AUCTION)
        client.post("/api/items", json={"auction_id": "1219987808"})
        resp = client.get("/api/items/1219987808")
        assert resp.status_code == 200
        assert resp.json()["auction_id"] == "1219987808"

    def test_get_item_not_found(self, client):
        resp = client.get("/api/items/nonexistent")
        assert resp.status_code == 404

    def test_update_item(self, client, mock_scraper):
        mock_scraper.fetch_auction = AsyncMock(return_value=MOCK_AUCTION)
        client.post("/api/items", json={"auction_id": "1219987808"})
        resp = client.put("/api/items/1219987808", json={"notes": "test note"})
        assert resp.status_code == 200
        assert resp.json()["notes"] == "test note"

    def test_delete_item(self, client, mock_scraper):
        mock_scraper.fetch_auction = AsyncMock(return_value=MOCK_AUCTION)
        client.post("/api/items", json={"auction_id": "1219987808"})
        resp = client.delete("/api/items/1219987808")
        assert resp.status_code == 204

        resp = client.get("/api/items/1219987808")
        assert resp.status_code == 404


class TestHealthServices:
    def test_services_list(self, client):
        resp = client.get("/api/health")
        data = resp.json()
        services = data["services"]
        names = [s["name"] for s in services]
        assert "database" in names
        assert "scheduler" in names

    def test_database_ok(self, client):
        resp = client.get("/api/health")
        services = {s["name"]: s for s in resp.json()["services"]}
        assert services["database"]["status"] == "ok"


class TestPagination:
    def test_items_pagination(self, client, mock_scraper):
        mock_scraper.fetch_auction = AsyncMock(return_value=MOCK_AUCTION)
        client.post("/api/items", json={"auction_id": "1219987808"})
        # limit=1, offset=0
        resp = client.get("/api/items?limit=1&offset=0")
        data = resp.json()
        assert len(data["items"]) == 1
        assert data["total"] == 1
        # offset past all items
        resp = client.get("/api/items?limit=10&offset=10")
        data = resp.json()
        assert len(data["items"]) == 0
        assert data["total"] == 1

    def test_keywords_pagination(self, client):
        # Create some keywords
        client.post("/api/keywords", json={"keyword": "test1"})
        client.post("/api/keywords", json={"keyword": "test2"})
        resp = client.get("/api/keywords?limit=1&offset=0")
        data = resp.json()
        assert len(data["keywords"]) == 1
        assert data["total"] == 2


class TestSearchErrorHandling:
    def test_search_scraper_error(self, client, mock_scraper):
        mock_scraper.search = AsyncMock(side_effect=Exception("connection timeout"))
        resp = client.get("/api/search?q=test")
        assert resp.status_code == 502
        assert "Yahoo search failed" in resp.json()["detail"]


class TestHistoryEndpoint:
    def test_get_history(self, client, mock_scraper):
        mock_scraper.fetch_auction = AsyncMock(return_value=MOCK_AUCTION)
        client.post("/api/items", json={"auction_id": "1219987808"})
        resp = client.get("/api/items/1219987808/history")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["change_type"] == "initial"
