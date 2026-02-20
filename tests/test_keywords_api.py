"""Tests for Watched Keywords API endpoints."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from yafuama.database import Base, get_db
from yafuama.main import app, app_state


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
def client(test_db):
    app_state["scraper"] = None
    app_state["scheduler"] = None
    yield TestClient(app, raise_server_exceptions=False)
    app_state.clear()


class TestKeywordsCRUD:
    def test_create_keyword(self, client):
        resp = client.post("/api/keywords", json={"keyword": "Nintendo Switch"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["keyword"] == "Nintendo Switch"
        assert data["is_active"] is True
        assert data["alert_count"] == 0

    def test_create_duplicate(self, client):
        client.post("/api/keywords", json={"keyword": "Pokemon"})
        resp = client.post("/api/keywords", json={"keyword": "Pokemon"})
        assert resp.status_code == 409

    def test_create_empty(self, client):
        resp = client.post("/api/keywords", json={"keyword": "  "})
        assert resp.status_code == 400

    def test_list_keywords(self, client):
        client.post("/api/keywords", json={"keyword": "Switch"})
        client.post("/api/keywords", json={"keyword": "Pokemon"})
        resp = client.get("/api/keywords")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["keywords"]) == 2

    def test_update_keyword(self, client):
        create_resp = client.post("/api/keywords", json={"keyword": "Test"})
        kw_id = create_resp.json()["id"]
        resp = client.put(f"/api/keywords/{kw_id}", json={"is_active": False})
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

    def test_update_not_found(self, client):
        resp = client.put("/api/keywords/999", json={"is_active": False})
        assert resp.status_code == 404

    def test_delete_keyword(self, client):
        create_resp = client.post("/api/keywords", json={"keyword": "Delete Me"})
        kw_id = create_resp.json()["id"]
        resp = client.delete(f"/api/keywords/{kw_id}")
        assert resp.status_code == 204

        # Verify gone
        list_resp = client.get("/api/keywords")
        assert list_resp.json()["total"] == 0

    def test_delete_not_found(self, client):
        resp = client.delete("/api/keywords/999")
        assert resp.status_code == 404

    def test_alerts_empty(self, client):
        create_resp = client.post("/api/keywords", json={"keyword": "Test"})
        kw_id = create_resp.json()["id"]
        resp = client.get(f"/api/keywords/{kw_id}/alerts")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


class TestScanEndpoints:
    def test_scan_all_no_scanner(self, client):
        resp = client.post("/api/keywords/scan")
        assert resp.status_code == 503

    def test_scan_keyword_no_scanner(self, client):
        create_resp = client.post("/api/keywords", json={"keyword": "Test"})
        kw_id = create_resp.json()["id"]
        resp = client.post(f"/api/keywords/{kw_id}/scan")
        assert resp.status_code == 503
