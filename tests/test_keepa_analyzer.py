"""Tests for Keepa analyzer pure functions."""

import pytest

from resell_trap.keepa.analyzer import (
    DEFAULT_GOOD_RANK_THRESHOLD,
    SalesRankAnalysis,
    UsedPriceAnalysis,
    analyze_product,
    analyze_sales_rank,
    analyze_used_price,
    recommend_price,
)


def _make_product(
    asin="B08XYZ",
    title="Test Product",
    current=None,
    avg30=None,
    avg90=None,
    min_vals=None,
    max_vals=None,
):
    """Build a mock Keepa product dict with stats."""
    stats = {}
    if current is not None:
        stats["current"] = current
    if avg30 is not None:
        stats["avg30"] = avg30
    if avg90 is not None:
        stats["avg90"] = avg90
    if min_vals is not None:
        stats["minInInterval"] = min_vals
    if max_vals is not None:
        stats["maxInInterval"] = max_vals
    return {"asin": asin, "title": title, "stats": stats}


class TestAnalyzeSalesRank:
    def test_no_data(self):
        product = {"asin": "X", "title": "X"}
        result = analyze_sales_rank(product)
        assert result.current_rank is None
        assert result.rank_trend == "unknown"
        assert result.sells_well is False

    def test_no_stats_key(self):
        product = {"asin": "X", "title": "X", "stats": {}}
        result = analyze_sales_rank(product)
        assert result.current_rank is None
        assert result.rank_trend == "unknown"

    def test_good_rank(self):
        product = _make_product(
            current=[-1, -1, -1, 50000],
            avg30=[-1, -1, -1, 50000],
            avg90=[-1, -1, -1, 55000],
        )
        result = analyze_sales_rank(product)
        assert result.current_rank == 50000
        assert result.sells_well is True

    def test_poor_rank(self):
        product = _make_product(current=[-1, -1, -1, 200000])
        result = analyze_sales_rank(product)
        assert result.current_rank == 200000
        assert result.sells_well is False

    def test_improving_trend(self):
        # 30d avg much lower than 90d avg -> improving
        product = _make_product(
            current=[-1, -1, -1, 40000],
            avg30=[-1, -1, -1, 40000],
            avg90=[-1, -1, -1, 60000],
        )
        result = analyze_sales_rank(product)
        assert result.rank_trend == "improving"

    def test_declining_trend(self):
        product = _make_product(
            current=[-1, -1, -1, 90000],
            avg30=[-1, -1, -1, 90000],
            avg90=[-1, -1, -1, 50000],
        )
        result = analyze_sales_rank(product)
        assert result.rank_trend == "declining"

    def test_stable_trend(self):
        product = _make_product(
            current=[-1, -1, -1, 50000],
            avg30=[-1, -1, -1, 50000],
            avg90=[-1, -1, -1, 50000],
        )
        result = analyze_sales_rank(product)
        assert result.rank_trend == "stable"

    def test_custom_threshold(self):
        product = _make_product(current=[-1, -1, -1, 50000])
        assert analyze_sales_rank(product).sells_well is True
        assert analyze_sales_rank(product, good_rank_threshold=30000).sells_well is False

    def test_sentinel_value_filtered(self):
        product = _make_product(current=[-1, -1, -1, -1])
        result = analyze_sales_rank(product)
        assert result.current_rank is None
        assert result.sells_well is False

    def test_min_max(self):
        product = _make_product(
            current=[-1, -1, -1, 50000],
            min_vals=[None, None, None, [12345, 20000]],  # [keepa_time, value]
            max_vals=[None, None, None, [67890, 120000]],
        )
        result = analyze_sales_rank(product)
        assert result.min_rank_90d == 20000
        assert result.max_rank_90d == 120000


class TestAnalyzeUsedPrice:
    def test_no_data(self):
        product = {"asin": "X", "title": "X"}
        result = analyze_used_price(product)
        assert result.current_price is None
        assert result.price_trend == "unknown"
        assert result.price_volatility == 0.0

    def test_rising_trend(self):
        product = _make_product(
            current=[-1, -1, 6000, -1],
            avg30=[-1, -1, 5500, -1],
            avg90=[-1, -1, 4500, -1],
        )
        result = analyze_used_price(product)
        assert result.current_price == 6000
        assert result.price_trend == "rising"

    def test_falling_trend(self):
        product = _make_product(
            current=[-1, -1, 3000, -1],
            avg30=[-1, -1, 3500, -1],
            avg90=[-1, -1, 5000, -1],
        )
        result = analyze_used_price(product)
        assert result.price_trend == "falling"

    def test_stable_trend(self):
        product = _make_product(
            current=[-1, -1, 5000, -1],
            avg30=[-1, -1, 5000, -1],
            avg90=[-1, -1, 5000, -1],
        )
        result = analyze_used_price(product)
        assert result.price_trend == "stable"

    def test_min_max_avg(self):
        product = _make_product(
            current=[-1, -1, 5000, -1],
            avg30=[-1, -1, 4800, -1],
            avg90=[-1, -1, 4500, -1],
            min_vals=[None, None, [0, 3000], None],  # [keepa_time, value]
            max_vals=[None, None, [0, 7000], None],
        )
        result = analyze_used_price(product)
        assert result.avg_price_30d == 4800
        assert result.avg_price_90d == 4500
        assert result.min_price_90d == 3000
        assert result.max_price_90d == 7000

    def test_volatility_calculated(self):
        product = _make_product(
            current=[-1, -1, 5000, -1],
            avg30=[-1, -1, 5000, -1],
            avg90=[-1, -1, 5000, -1],
            min_vals=[None, None, [0, 2000], None],  # [keepa_time, value]
            max_vals=[None, None, [0, 8000], None],
        )
        result = analyze_used_price(product)
        # volatility = (8000 - 2000) / 5000 = 1.2
        assert result.price_volatility == 1.2


class TestRecommendPrice:
    def _no_data_rank(self):
        return SalesRankAnalysis(
            current_rank=None, avg_rank_30d=None, avg_rank_90d=None,
            min_rank_90d=None, max_rank_90d=None,
            rank_trend="unknown", sells_well=False,
            rank_threshold_used=DEFAULT_GOOD_RANK_THRESHOLD,
        )

    def _no_data_used(self):
        return UsedPriceAnalysis(
            current_price=None, avg_price_30d=None, avg_price_90d=None,
            min_price_90d=None, max_price_90d=None,
            price_trend="unknown", price_volatility=0.0,
        )

    def _good_rank(self):
        return SalesRankAnalysis(
            current_rank=50000, avg_rank_30d=50000, avg_rank_90d=55000,
            min_rank_90d=30000, max_rank_90d=80000,
            rank_trend="stable", sells_well=True,
            rank_threshold_used=DEFAULT_GOOD_RANK_THRESHOLD,
        )

    def _poor_rank(self):
        return SalesRankAnalysis(
            current_rank=200000, avg_rank_30d=200000, avg_rank_90d=200000,
            min_rank_90d=150000, max_rank_90d=250000,
            rank_trend="stable", sells_well=False,
            rank_threshold_used=DEFAULT_GOOD_RANK_THRESHOLD,
        )

    def _market_used(self):
        return UsedPriceAnalysis(
            current_price=6000, avg_price_30d=5800, avg_price_90d=5500,
            min_price_90d=4500, max_price_90d=7000,
            price_trend="stable", price_volatility=0.3,
        )

    def test_margin_based_fallback(self):
        rec = recommend_price(self._no_data_rank(), self._no_data_used(), cost_price=3000, shipping_cost=800)
        assert rec.strategy == "margin_based"
        # floor = ceil(3800 / 0.75 / 10) * 10 = 5070
        assert rec.recommended_price == 5070

    def test_undercut_when_sells_well(self):
        rec = recommend_price(self._good_rank(), self._market_used(), cost_price=3000, shipping_cost=800)
        assert rec.strategy in ("undercut", "competitive")
        assert rec.recommended_price >= 5070  # never below floor

    def test_market_average_poor_sales(self):
        rec = recommend_price(self._poor_rank(), self._market_used(), cost_price=3000, shipping_cost=800)
        assert rec.strategy == "market_average"

    def test_floor_price_respected(self):
        cheap_used = UsedPriceAnalysis(
            current_price=2000, avg_price_30d=1800, avg_price_90d=1500,
            min_price_90d=1000, max_price_90d=2500,
            price_trend="stable", price_volatility=0.2,
        )
        rec = recommend_price(self._good_rank(), cheap_used, cost_price=3000, shipping_cost=800)
        assert rec.recommended_price >= 5070

    def test_zero_cost_price(self):
        rec = recommend_price(self._no_data_rank(), self._no_data_used(), cost_price=0)
        assert rec.recommended_price == 0
        assert rec.strategy == "margin_based"

    def test_high_confidence_low_volatility(self):
        low_vol_used = UsedPriceAnalysis(
            current_price=6000, avg_price_30d=5800, avg_price_90d=5500,
            min_price_90d=5000, max_price_90d=6500,
            price_trend="stable", price_volatility=0.2,
        )
        rec = recommend_price(self._good_rank(), low_vol_used, cost_price=3000, shipping_cost=800)
        assert rec.confidence == "high"

    def test_japanese_reasoning(self):
        rec = recommend_price(self._good_rank(), self._market_used(), cost_price=3000, shipping_cost=800)
        assert "売れ行き" in rec.reasoning


class TestAnalyzeProduct:
    def test_full_analysis(self):
        product = _make_product(
            asin="B08XYZ",
            title="Test",
            current=[-1, -1, 5500, 45000],
            avg30=[-1, -1, 5200, 42000],
            avg90=[-1, -1, 5000, 55000],
        )
        result = analyze_product(product, cost_price=3000, shipping_cost=800)
        assert result.asin == "B08XYZ"
        assert result.sales_rank.current_rank == 45000
        assert result.used_price.current_price == 5500
        assert result.recommendation is not None
        assert result.recommendation.recommended_price > 0

    def test_no_recommendation_without_cost(self):
        product = _make_product(current=[-1, -1, 5000, 50000])
        result = analyze_product(product, cost_price=0)
        assert result.recommendation is None

    def test_empty_product(self):
        result = analyze_product({"asin": "X", "title": "X"})
        assert result.sales_rank.current_rank is None
        assert result.used_price.current_price is None
        assert result.recommendation is None
