"""Tests for AI discovery engine and API endpoints."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from yafuama.ai.generator import CandidateProposal
from yafuama.ai.validator import ValidationResult, should_auto_add
from yafuama.database import Base, get_db
from yafuama.main import app, app_state
from yafuama.models import DealAlert, DiscoveryLog, KeywordCandidate, WatchedKeyword


# --- Validator tests ---


class TestShouldAutoAdd:
    def test_auto_add_when_all_conditions_met(self):
        proposal = CandidateProposal(
            keyword="test", strategy="brand", confidence=0.7,
            parent_keyword_id=None, reasoning="test",
        )
        result = ValidationResult(
            is_valid=True, potential_deals=3, best_profit=6000,
        )
        assert should_auto_add(proposal, result, threshold=0.6) is True

    def test_no_auto_add_low_confidence(self):
        proposal = CandidateProposal(
            keyword="test", strategy="brand", confidence=0.4,
            parent_keyword_id=None, reasoning="test",
        )
        result = ValidationResult(
            is_valid=True, potential_deals=3, best_profit=6000,
        )
        assert should_auto_add(proposal, result, threshold=0.6) is False

    def test_no_auto_add_low_profit(self):
        proposal = CandidateProposal(
            keyword="test", strategy="brand", confidence=0.7,
            parent_keyword_id=None, reasoning="test",
        )
        result = ValidationResult(
            is_valid=True, potential_deals=3, best_profit=3000,
        )
        assert should_auto_add(proposal, result, threshold=0.6) is False

    def test_no_auto_add_invalid(self):
        proposal = CandidateProposal(
            keyword="test", strategy="brand", confidence=0.7,
            parent_keyword_id=None, reasoning="test",
        )
        result = ValidationResult(is_valid=False)
        assert should_auto_add(proposal, result, threshold=0.6) is False


class TestValidationResult:
    def test_to_json(self):
        result = ValidationResult(
            is_valid=True, yahoo_result_count=15,
            keepa_result_count=5, potential_deals=3,
            best_margin=52.0, best_profit=6000,
        )
        json_str = result.to_json()
        assert '"yahoo_count": 15' in json_str
        assert '"best_profit": 6000' in json_str


# --- API tests ---


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
def mock_services():
    scraper = AsyncMock()
    app_state["scraper"] = scraper
    app_state["scheduler"] = None
    app_state["discovery_engine"] = AsyncMock()
    yield
    app_state.clear()


@pytest.fixture()
def client(test_db, mock_services):
    return TestClient(app, raise_server_exceptions=False)


class TestDiscoveryStatusEndpoint:
    def test_status_when_enabled(self, client):
        resp = client.get("/api/discovery/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "enabled" in data
        assert "total_ai_keywords" in data
        assert "pending_candidates" in data

    def test_status_with_log(self, client, test_db):
        Session = test_db
        db = Session()
        log = DiscoveryLog(
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            status="completed",
            candidates_generated=5,
            keywords_added=2,
        )
        db.add(log)
        db.commit()
        db.close()

        resp = client.get("/api/discovery/status")
        data = resp.json()
        assert data["last_cycle"] is not None
        assert data["last_cycle"]["candidates_generated"] == 5


class TestDiscoveryCandidatesEndpoint:
    def test_list_candidates(self, client, test_db):
        Session = test_db
        db = Session()
        db.add(KeywordCandidate(
            keyword="test keyword", strategy="brand", confidence=0.7,
            status="validated", reasoning="test reason",
        ))
        db.commit()
        db.close()

        resp = client.get("/api/discovery/candidates")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["candidates"][0]["keyword"] == "test keyword"

    def test_approve_candidate(self, client, test_db):
        Session = test_db
        db = Session()
        kc = KeywordCandidate(
            keyword="new keyword", strategy="brand", confidence=0.7,
            status="validated", reasoning="good candidate",
        )
        db.add(kc)
        db.commit()
        kc_id = kc.id
        db.close()

        resp = client.post(f"/api/discovery/candidates/{kc_id}/approve")
        assert resp.status_code == 200

        # Check keyword was created
        db = Session()
        kw = db.query(WatchedKeyword).filter(WatchedKeyword.keyword == "new keyword").first()
        assert kw is not None
        assert kw.source == "ai_brand"
        kc = db.get(KeywordCandidate, kc_id)
        assert kc.status == "approved"
        db.close()

    def test_reject_candidate(self, client, test_db):
        Session = test_db
        db = Session()
        kc = KeywordCandidate(
            keyword="bad keyword", strategy="title", confidence=0.3,
            status="validated", reasoning="test",
        )
        db.add(kc)
        db.commit()
        kc_id = kc.id
        db.close()

        resp = client.post(f"/api/discovery/candidates/{kc_id}/reject")
        assert resp.status_code == 200

        db = Session()
        kc = db.get(KeywordCandidate, kc_id)
        assert kc.status == "rejected"
        db.close()


class TestCandidateDeduplication:
    def test_approve_auto_rejects_similar(self, client, test_db):
        """Approving a candidate should auto-reject similar pending ones."""
        Session = test_db
        db = Session()
        kc1 = KeywordCandidate(
            keyword="Sony ヘッドホン WH-1000", strategy="brand", confidence=0.7,
            status="validated", reasoning="main",
        )
        kc2 = KeywordCandidate(
            keyword="ソニー ヘッドホン WH-1000", strategy="synonym", confidence=0.5,
            status="pending", reasoning="similar",
        )
        kc3 = KeywordCandidate(
            keyword="Nintendo Switch コントローラー", strategy="brand", confidence=0.6,
            status="pending", reasoning="different",
        )
        db.add_all([kc1, kc2, kc3])
        db.commit()
        id1, id2, id3 = kc1.id, kc2.id, kc3.id
        db.close()

        resp = client.post(f"/api/discovery/candidates/{id1}/approve")
        assert resp.status_code == 200
        data = resp.json()
        assert data["auto_rejected"] >= 1

        db = Session()
        assert db.get(KeywordCandidate, id2).status == "rejected"  # similar → auto-rejected
        assert db.get(KeywordCandidate, id3).status == "pending"   # different → untouched
        db.close()

    def test_list_excludes_similar_to_existing(self, client, test_db):
        """Candidates similar to existing WatchedKeywords should not appear."""
        Session = test_db
        db = Session()
        # Existing keyword
        db.add(WatchedKeyword(keyword="Sony ヘッドホン WH-1000", is_active=True))
        # Candidate similar to existing
        db.add(KeywordCandidate(
            keyword="ソニー ヘッドホン WH-1000", strategy="synonym", confidence=0.6,
            status="pending", reasoning="similar to existing",
        ))
        # Candidate that's genuinely new
        db.add(KeywordCandidate(
            keyword="Panasonic ドライヤー", strategy="brand", confidence=0.7,
            status="validated", reasoning="new",
        ))
        db.commit()
        db.close()

        resp = client.get("/api/discovery/candidates")
        assert resp.status_code == 200
        data = resp.json()
        keywords = [c["keyword"] for c in data["candidates"]]
        assert "Panasonic ドライヤー" in keywords
        assert "ソニー ヘッドホン WH-1000" not in keywords

    def test_list_deduplicates_within_candidates(self, client, test_db):
        """Similar candidates in the list should be deduplicated (keep highest confidence)."""
        Session = test_db
        db = Session()
        db.add(KeywordCandidate(
            keyword="Canon カメラ EOS", strategy="brand", confidence=0.8,
            status="validated", reasoning="high conf",
        ))
        db.add(KeywordCandidate(
            keyword="キャノン カメラ EOS", strategy="synonym", confidence=0.5,
            status="pending", reasoning="low conf duplicate",
        ))
        db.commit()
        db.close()

        resp = client.get("/api/discovery/candidates")
        data = resp.json()
        # Only one should remain (the higher confidence one comes first due to ordering)
        assert data["total"] == 1
        assert data["candidates"][0]["keyword"] == "Canon カメラ EOS"


class TestDiscoveryLogEndpoint:
    def test_list_logs(self, client, test_db):
        Session = test_db
        db = Session()
        for i in range(3):
            db.add(DiscoveryLog(
                started_at=datetime.now(timezone.utc),
                status="completed",
                candidates_generated=i * 2,
            ))
        db.commit()
        db.close()

        resp = client.get("/api/discovery/log")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
