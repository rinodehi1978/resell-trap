"""Tests for AI keyword discovery analyzer."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from yafuama.ai.analyzer import (
    BrandPattern,
    KeywordInsights,
    analyze_deal_history,
    compute_performance_score,
    extract_brand_patterns,
    extract_price_ranges,
    extract_product_types,
    extract_title_tokens,
)
from yafuama.database import Base
from yafuama.models import DealAlert, WatchedKeyword


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _make_keyword(db, keyword="test", source="manual", scans=5, deals=2, profit=10000):
    kw = WatchedKeyword(
        keyword=keyword,
        source=source,
        total_scans=scans,
        total_deals_found=deals,
        total_gross_profit=profit,
    )
    db.add(kw)
    db.flush()
    return kw


def _make_alert(db, kw, title="Nintendo Switch 本体", yahoo_price=5000,
                sell_price=12000, profit=5000, margin=45.0, asin="B08XYZ"):
    alert = DealAlert(
        keyword_id=kw.id,
        yahoo_auction_id=f"yahoo-{id(title)}",
        amazon_asin=asin,
        yahoo_title=title,
        yahoo_url="https://example.com",
        yahoo_price=yahoo_price,
        sell_price=sell_price,
        gross_profit=profit,
        gross_margin_pct=margin,
        notified_at=datetime.now(timezone.utc),
    )
    db.add(alert)
    db.flush()
    return alert


class TestPerformanceScore:
    def test_zero_scans(self, db):
        kw = _make_keyword(db, scans=0, deals=0, profit=0)
        score = compute_performance_score(kw, [])
        assert score == 0.0

    def test_high_performer(self, db):
        kw = _make_keyword(db, scans=10, deals=8, profit=60000)
        alerts = [
            _make_alert(db, kw, title=f"item {i}", profit=7500, margin=55.0)
            for i in range(8)
        ]
        score = compute_performance_score(kw, alerts)
        assert score > 0.5

    def test_low_performer(self, db):
        kw = _make_keyword(db, scans=20, deals=1, profit=3000)
        alert = _make_alert(db, kw, profit=3000, margin=40.0)
        alert.notified_at = datetime.now(timezone.utc) - timedelta(days=30)
        score = compute_performance_score(kw, [alert])
        assert score < 0.25

    def test_recency_bonus(self, db):
        kw = _make_keyword(db, scans=5, deals=2, profit=10000)
        recent_alert = _make_alert(db, kw, title="recent", profit=5000, margin=50.0)
        recent_alert.notified_at = datetime.now(timezone.utc) - timedelta(days=3)
        score_recent = compute_performance_score(kw, [recent_alert])

        old_alert = _make_alert(db, kw, title="old", profit=5000, margin=50.0, asin="B09OLD")
        old_alert.notified_at = datetime.now(timezone.utc) - timedelta(days=20)
        score_old = compute_performance_score(kw, [old_alert])

        assert score_recent > score_old


class TestBrandExtraction:
    def test_detects_known_brands(self, db):
        kw = _make_keyword(db, keyword="nintendo switch")
        alerts = [
            _make_alert(db, kw, title="Nintendo Switch 有機EL", profit=5000),
            _make_alert(db, kw, title="Nintendo Switch Lite", profit=4000, asin="B09A"),
            _make_alert(db, kw, title="Nintendo Switch Pro Controller", profit=3000, asin="B09B"),
        ]
        brands = extract_brand_patterns(alerts, {kw.id: kw})
        assert len(brands) >= 1
        assert brands[0].brand_name == "nintendo"
        assert brands[0].deal_count == 3

    def test_ignores_single_deal_brands(self, db):
        kw = _make_keyword(db, keyword="misc")
        alerts = [
            _make_alert(db, kw, title="Dyson V15 掃除機", profit=8000),
        ]
        brands = extract_brand_patterns(alerts, {kw.id: kw})
        assert len(brands) == 0  # Only 1 deal, filtered out


class TestProductTypeExtraction:
    def test_extracts_frequent_tokens(self, db):
        kw = _make_keyword(db, keyword="test")
        titles = [
            "Pokemon Card BOX 未開封",
            "Pokemon Card SR レア",
            "Pokemon Card GX セット",
        ]
        alerts = [
            _make_alert(db, kw, title=t, profit=5000, asin=f"A{i}")
            for i, t in enumerate(titles)
        ]
        types = extract_product_types(alerts)
        type_names = [t.product_type for t in types]
        assert "card" in type_names

    def test_filters_stopwords(self, db):
        kw = _make_keyword(db, keyword="test")
        alerts = [
            _make_alert(db, kw, title="送料無料 中古 美品 item", profit=5000, asin=f"X{i}")
            for i in range(5)
        ]
        types = extract_product_types(alerts)
        type_names = [t.product_type for t in types]
        assert "送料" not in type_names
        assert "無料" not in type_names
        assert "中古" not in type_names


class TestPriceRanges:
    def test_buckets_deals(self, db):
        kw = _make_keyword(db, keyword="test")
        alerts = [
            _make_alert(db, kw, title="cheap", yahoo_price=2000, profit=3000, margin=50.0, asin="A1"),
            _make_alert(db, kw, title="mid", yahoo_price=7000, profit=5000, margin=45.0, asin="A2"),
            _make_alert(db, kw, title="expensive", yahoo_price=25000, profit=10000, margin=40.0, asin="A3"),
        ]
        ranges = extract_price_ranges(alerts)
        labels = [r.range_label for r in ranges]
        assert "0-3000" in labels
        assert "5000-10000" in labels
        assert "10000-30000" in labels


class TestTitleTokens:
    def test_scores_tokens(self, db):
        kw = _make_keyword(db, keyword="test")
        alerts = [
            _make_alert(db, kw, title="Pokemon Card BOX", profit=5000, asin="A1"),
            _make_alert(db, kw, title="Pokemon Card SR", profit=6000, asin="A2"),
            _make_alert(db, kw, title="Dragon Ball Card", profit=4000, asin="A3"),
        ]
        tokens = extract_title_tokens(alerts)
        assert "card" in tokens
        assert tokens["card"] > 0


class TestAnalyzeDealHistory:
    def test_full_analysis(self, db):
        kw = _make_keyword(db, keyword="Nintendo Switch", scans=10, deals=3, profit=15000)
        for i in range(3):
            _make_alert(db, kw, title=f"Nintendo Switch Item {i}",
                       profit=5000, margin=50.0, asin=f"B{i}")
        db.commit()

        insights = analyze_deal_history(db)
        assert isinstance(insights, KeywordInsights)
        assert insights.total_deals == 3
        assert insights.total_keywords == 1
        assert len(insights.top_keywords) == 1
        assert insights.top_keywords[0].keyword == "Nintendo Switch"

    def test_empty_db(self, db):
        insights = analyze_deal_history(db)
        assert insights.total_deals == 0
        assert insights.total_keywords == 0
