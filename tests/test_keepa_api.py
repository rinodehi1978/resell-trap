"""Tests for Keepa API endpoints."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from yafuama.keepa import KeepaApiError
from yafuama.main import app, app_state


def _make_mock_product(asin="B08XYZ", title="Test Product"):
    """Return a mock Keepa product dict with realistic stats."""
    return {
        "asin": asin,
        "title": title,
        "stats": {
            "current": [-1, -1, 5800, 45000],
            "avg30": [-1, -1, 5500, 42000],
            "avg90": [-1, -1, 5200, 55000],
            "minInInterval": [None, None, [0, 4200], [0, 20000]],
            "maxInInterval": [None, None, [0, 7000], [0, 120000]],
        },
    }


@pytest.fixture()
def mock_keepa_client():
    client = AsyncMock()
    client.query_product = AsyncMock(return_value=_make_mock_product())
    app_state["keepa"] = client
    return client


@pytest.fixture()
def client(mock_keepa_client):
    app_state["scraper"] = AsyncMock()
    app_state["scheduler"] = None
    c = TestClient(app, raise_server_exceptions=False)
    yield c
    app_state.clear()


class TestKeepaAnalysisGet:
    def test_basic_analysis(self, client, mock_keepa_client):
        resp = client.get("/api/keepa/analysis/B08XYZ")
        assert resp.status_code == 200
        data = resp.json()
        assert data["asin"] == "B08XYZ"
        assert data["title"] == "Test Product"
        assert data["recommendation"] is None  # no cost_price

    def test_with_cost_price(self, client, mock_keepa_client):
        resp = client.get("/api/keepa/analysis/B08XYZ?cost_price=3000&shipping_cost=800")
        assert resp.status_code == 200
        data = resp.json()
        assert data["recommendation"] is not None
        assert data["recommendation"]["recommended_price"] > 0
        assert data["recommendation"]["strategy"] in ("undercut", "competitive", "market_average", "margin_based")

    def test_sales_rank_fields(self, client, mock_keepa_client):
        resp = client.get("/api/keepa/analysis/B08XYZ")
        sr = resp.json()["sales_rank"]
        assert sr["current_rank"] == 45000
        assert sr["avg_rank_30d"] == 42000
        assert sr["avg_rank_90d"] == 55000
        assert sr["rank_trend"] == "improving"
        assert sr["sells_well"] is True

    def test_used_price_fields(self, client, mock_keepa_client):
        resp = client.get("/api/keepa/analysis/B08XYZ")
        up = resp.json()["used_price"]
        assert up["current_price"] == 5800
        assert up["avg_price_30d"] == 5500
        assert up["avg_price_90d"] == 5200
        assert up["price_trend"] in ("rising", "stable")  # 5500/5200=1.058, below 1.10 threshold
        assert up["price_volatility"] > 0

    def test_keepa_error(self, client, mock_keepa_client):
        mock_keepa_client.query_product.side_effect = KeepaApiError("token exhausted", tokens_left=0)
        resp = client.get("/api/keepa/analysis/B08XYZ")
        assert resp.status_code == 502
        assert "Keepa API error" in resp.json()["detail"]


class TestKeepaAnalysisPost:
    def test_post_analysis(self, client, mock_keepa_client):
        resp = client.post("/api/keepa/analysis", json={
            "asin": "B08XYZ",
            "cost_price": 3000,
            "shipping_cost": 800,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["asin"] == "B08XYZ"
        assert data["recommendation"] is not None

    def test_post_without_cost(self, client, mock_keepa_client):
        resp = client.post("/api/keepa/analysis", json={"asin": "B08XYZ"})
        assert resp.status_code == 200
        assert resp.json()["recommendation"] is None


class TestKeepaNotConfigured:
    def test_503_when_not_configured(self):
        app_state.pop("keepa", None)
        app_state["scraper"] = AsyncMock()
        app_state["scheduler"] = None
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.get("/api/keepa/analysis/B08XYZ")
        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"]
        app_state.clear()
