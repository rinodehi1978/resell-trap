"""Tests for AI keyword generator strategies."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from yafuama.ai.analyzer import (
    BrandPattern,
    KeywordInsights,
    KeywordPerformance,
    PriceRangePattern,
    ProductTypePattern,
)
from yafuama.ai.generator import (
    generate_all,
    generate_brand_expansion,
    generate_category_keywords,
    generate_demand,
    generate_synonyms,
    generate_title_decomp,
)
from yafuama.database import Base
from yafuama.models import WatchedKeyword


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


def _make_insights(**overrides) -> KeywordInsights:
    defaults = dict(
        top_keywords=[
            KeywordPerformance(
                keyword_id=1, keyword="Nintendo Switch", total_deals=5,
                total_scans=10, avg_gross_profit=6000, avg_gross_margin=50.0,
                performance_score=0.6, source="manual",
            ),
        ],
        brand_patterns=[
            BrandPattern(
                brand_name="nintendo", deal_count=5, avg_profit=6000,
                total_profit=30000, example_keywords=["Nintendo Switch"],
            ),
        ],
        product_type_patterns=[
            ProductTypePattern(product_type="switch", deal_count=5, avg_profit=6000, score=3.0),
            ProductTypePattern(product_type="card", deal_count=4, avg_profit=5000, score=2.5),
            ProductTypePattern(product_type="controller", deal_count=3, avg_profit=4000, score=2.0),
        ],
        price_range_patterns=[
            PriceRangePattern(range_label="3000-5000", min_price=3000, max_price=5000, deal_count=10, avg_margin=48.0),
        ],
        title_tokens={
            "pokemon": 4.0, "card": 3.5, "box": 2.0, "switch": 3.0,
            "controller": 2.0, "pro": 1.5,
        },
        total_deals=20,
        total_keywords=3,
    )
    defaults.update(overrides)
    return KeywordInsights(**defaults)


class TestBrandExpansion:
    def test_generates_brand_x_product_type(self):
        insights = _make_insights()
        candidates = generate_brand_expansion(insights, set(), max_count=10)
        assert len(candidates) > 0
        # All should be brand strategy
        for c in candidates:
            assert c.strategy == "brand"
            assert c.confidence == 0.7
            assert "nintendo" in c.keyword.lower()

    def test_skips_existing_keywords(self):
        insights = _make_insights()
        existing = {"nintendo card", "nintendo controller"}
        candidates = generate_brand_expansion(insights, existing, max_count=10)
        kws = {c.keyword.lower() for c in candidates}
        assert "nintendo card" not in kws
        assert "nintendo controller" not in kws

    def test_skips_low_profit_brands(self):
        insights = _make_insights(
            brand_patterns=[
                BrandPattern(brand_name="cheap", deal_count=5, avg_profit=1000,
                            total_profit=5000, example_keywords=["cheap stuff"]),
            ]
        )
        candidates = generate_brand_expansion(insights, set(), max_count=10)
        assert len(candidates) == 0  # avg_profit < 3000


class TestTitleDecomp:
    def test_generates_token_combinations(self):
        insights = _make_insights()
        candidates = generate_title_decomp(insights, set(), max_count=10)
        assert len(candidates) > 0
        for c in candidates:
            assert c.strategy == "title"
            assert c.confidence == 0.6
            # Should be 2-word combinations
            assert " " in c.keyword

    def test_not_enough_tokens(self):
        insights = _make_insights(title_tokens={"solo": 2.0})
        candidates = generate_title_decomp(insights, set(), max_count=10)
        assert len(candidates) == 0


class TestCategoryKeywords:
    def test_generates_brand_condition_variants(self):
        insights = _make_insights()
        candidates = generate_category_keywords(insights, set(), max_count=20)
        assert len(candidates) > 0
        for c in candidates:
            assert c.strategy == "category"
            assert "nintendo" in c.keyword.lower()


class TestSynonyms:
    def test_generates_synonym_variants(self):
        insights = _make_insights()
        candidates = generate_synonyms(insights, set(), max_count=10)
        # Should generate katakana/english variants
        assert len(candidates) > 0
        for c in candidates:
            assert c.strategy == "synonym"
            assert c.confidence == 0.5


class TestGenerateDemand:
    def test_generates_from_model_field(self):
        """Products with model field generate brand+model keywords."""
        products = [
            {
                "model": "WH-1000XM4",
                "brand": "Sony",
                "title": "Sony WH-1000XM4 Wireless Headphones",
                "stats": {"salesRankDrops30": 10},
            },
        ]
        candidates = generate_demand(products, set(), max_count=10)
        assert len(candidates) == 1
        assert candidates[0].keyword == "Sony WH-1000XM4"
        assert candidates[0].strategy == "demand"
        assert candidates[0].confidence == 0.80

    def test_extracts_model_from_title_when_no_model_field(self):
        """Falls back to title extraction when model field is empty."""
        products = [
            {
                "model": "",
                "brand": "Casio",
                "title": "Casio XD900 Electronic Dictionary",
                "stats": {"salesRankDrops30": 7},
            },
        ]
        candidates = generate_demand(products, set(), max_count=10)
        assert len(candidates) == 1
        assert "xd900" in candidates[0].keyword.lower()

    def test_skips_product_without_model(self):
        """Products without model number are skipped."""
        products = [
            {
                "model": "",
                "brand": "Generic",
                "title": "Some Product Without Model Number",
                "stats": {"salesRankDrops30": 5},
            },
        ]
        candidates = generate_demand(products, set(), max_count=10)
        assert len(candidates) == 0

    def test_skips_existing_keywords(self):
        """Existing keywords are not duplicated."""
        products = [
            {
                "model": "WH-1000XM4",
                "brand": "Sony",
                "title": "Sony WH-1000XM4",
                "stats": {"salesRankDrops30": 10},
            },
        ]
        existing = {"sony wh-1000xm4"}
        candidates = generate_demand(products, existing, max_count=10)
        assert len(candidates) == 0

    def test_respects_max_count(self):
        """Output is capped at max_count."""
        products = [
            {"model": f"MODEL{i}", "brand": "Brand", "title": f"Product {i}", "stats": {}}
            for i in range(20)
        ]
        candidates = generate_demand(products, set(), max_count=3)
        assert len(candidates) == 3

    def test_reasoning_includes_drops(self):
        """Reasoning mentions the sales drop count."""
        products = [
            {
                "model": "ABC123",
                "brand": "Test",
                "title": "Test ABC123",
                "stats": {"salesRankDrops30": 15},
            },
        ]
        candidates = generate_demand(products, set())
        assert "15" in candidates[0].reasoning

    def test_empty_products_list(self):
        """Empty input returns empty output."""
        candidates = generate_demand([], set())
        assert candidates == []


class TestGenerateAll:
    def test_returns_deduplicated_candidates(self, db):
        # Add one existing keyword
        db.add(WatchedKeyword(keyword="Nintendo Switch"))
        db.commit()

        insights = _make_insights()
        candidates = generate_all(insights, db, max_per_strategy=5)
        # Should have candidates but no duplicates
        kws = [c.keyword.lower() for c in candidates]
        assert len(kws) == len(set(kws))
        # Should not include existing keyword
        assert "nintendo switch" not in kws
