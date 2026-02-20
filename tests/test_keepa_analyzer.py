"""Tests for Keepa analyzer pure functions."""

import pytest

from yafuama.keepa.analyzer import (
    DEFAULT_GOOD_RANK_THRESHOLD,
    SYSTEM_FEE,
    DealCandidate,
    SalesRankAnalysis,
    UsedPriceAnalysis,
    analyze_product,
    analyze_sales_rank,
    analyze_used_price,
    calculate_forwarding_cost,
    recommend_price,
    score_deal,
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


class TestCalculateForwardingCost:
    """Tests for size-based forwarding cost calculation."""

    def test_60_size(self):
        """3-side total <= 600mm → 60 size = 735 yen."""
        product = {"packageHeight": 100, "packageLength": 200, "packageWidth": 250}
        assert calculate_forwarding_cost(product) == 735

    def test_80_size(self):
        """3-side total <= 800mm → 80 size = 840 yen."""
        product = {"packageHeight": 200, "packageLength": 300, "packageWidth": 250}
        assert calculate_forwarding_cost(product) == 840

    def test_100_size(self):
        """3-side total <= 1000mm → 100 size = 960 yen."""
        product = {"packageHeight": 300, "packageLength": 350, "packageWidth": 300}
        assert calculate_forwarding_cost(product) == 960

    def test_120_size(self):
        product = {"packageHeight": 400, "packageLength": 400, "packageWidth": 350}
        assert calculate_forwarding_cost(product) == 1150

    def test_140_size(self):
        product = {"packageHeight": 500, "packageLength": 500, "packageWidth": 350}
        assert calculate_forwarding_cost(product) == 1340

    def test_160_size(self):
        product = {"packageHeight": 500, "packageLength": 550, "packageWidth": 500}
        assert calculate_forwarding_cost(product) == 1810

    def test_180_size(self):
        product = {"packageHeight": 600, "packageLength": 600, "packageWidth": 500}
        assert calculate_forwarding_cost(product) == 3060

    def test_200_size(self):
        product = {"packageHeight": 700, "packageLength": 700, "packageWidth": 500}
        assert calculate_forwarding_cost(product) == 3810

    def test_over_200_returns_none(self):
        """3-side total > 2000mm → None (not supported)."""
        product = {"packageHeight": 800, "packageLength": 800, "packageWidth": 500}
        assert calculate_forwarding_cost(product) is None

    def test_no_dimensions_returns_none(self):
        """No package dimensions → None."""
        product = {}
        assert calculate_forwarding_cost(product) is None

    def test_partial_dimensions_returns_none(self):
        """Only some dimensions present → None."""
        product = {"packageHeight": 200, "packageLength": 300}
        assert calculate_forwarding_cost(product) is None

    def test_zero_dimension_returns_none(self):
        """Zero dimension → None."""
        product = {"packageHeight": 0, "packageLength": 300, "packageWidth": 200}
        assert calculate_forwarding_cost(product) is None

    def test_boundary_600(self):
        """Exactly 600mm → 60 size."""
        product = {"packageHeight": 200, "packageLength": 200, "packageWidth": 200}
        assert calculate_forwarding_cost(product) == 735

    def test_boundary_601(self):
        """601mm → 80 size."""
        product = {"packageHeight": 201, "packageLength": 200, "packageWidth": 200}
        assert calculate_forwarding_cost(product) == 840


class TestScoreDeal:
    """Tests for the deal scoring / gross margin calculation.

    New cost formula: total_cost = yahoo_price + yahoo_shipping + forwarding + SYSTEM_FEE(100)
    inspection_fee is deprecated; SYSTEM_FEE is always 100 yen.
    """

    def _product_with_used(self, used_price, rank=50000, **extra):
        """Make a Keepa product with used price and rank."""
        product = _make_product(
            current=[-1, -1, used_price, rank],
            avg30=[-1, -1, used_price, rank],
            avg90=[-1, -1, used_price, rank],
        )
        product.update(extra)
        return product

    def test_basic_profit_calculation(self):
        """Yahoo 3000, shipping 0, forwarding fallback 800, system_fee 100.
        Amazon used 10000, fee 10% = 1000.
        total_cost = 3000 + 0 + 800 + 100 = 3900
        gross_profit = 10000 - 3900 - 1000 = 5100
        gross_margin = 5100 / 10000 * 100 = 51.0%
        """
        deal = score_deal(
            yahoo_price=3000,
            keepa_product=self._product_with_used(10000),
            yahoo_shipping=0,
            forwarding_cost=800,
            amazon_fee_pct=10.0,
        )
        assert deal is not None
        assert deal.total_cost == 3900
        assert deal.amazon_fee == 1000
        assert deal.gross_profit == 5100
        assert deal.gross_margin_pct == 51.0
        assert deal.sell_price == 10000
        assert deal.system_fee == SYSTEM_FEE

    def test_with_yahoo_shipping(self):
        """Yahoo shipping adds to total cost."""
        deal = score_deal(
            yahoo_price=3000,
            keepa_product=self._product_with_used(10000),
            yahoo_shipping=1000,
            forwarding_cost=800,
        )
        assert deal is not None
        assert deal.total_cost == 3000 + 1000 + 800 + SYSTEM_FEE  # 4900
        assert deal.yahoo_shipping == 1000

    def test_no_price_returns_none(self):
        """No used or new price -> None."""
        product = _make_product(current=[-1, -1, -1, 50000])
        deal = score_deal(yahoo_price=3000, keepa_product=product)
        assert deal is None

    def test_margin_threshold_filtering(self):
        """Low-margin deals still returned (filtering is in views, not here)."""
        deal = score_deal(
            yahoo_price=8000,
            keepa_product=self._product_with_used(10000),
        )
        assert deal is not None
        assert deal.gross_profit < 0

    def test_free_shipping_deal(self):
        """Free Yahoo shipping = 0 cost component."""
        deal = score_deal(
            yahoo_price=2000,
            keepa_product=self._product_with_used(8000),
            yahoo_shipping=0,
            forwarding_cost=500,
        )
        assert deal is not None
        assert deal.total_cost == 2000 + 0 + 500 + SYSTEM_FEE  # 2600
        assert deal.amazon_fee == 800  # 10% of 8000
        assert deal.gross_profit == 8000 - 2600 - 800  # 4600

    def test_cost_breakdown_fields(self):
        """All cost fields are correctly populated."""
        deal = score_deal(
            yahoo_price=5000,
            keepa_product=self._product_with_used(15000),
            yahoo_shipping=500,
            forwarding_cost=1000,
            amazon_fee_pct=10.0,
        )
        assert deal is not None
        assert deal.forwarding_cost == 1000
        assert deal.system_fee == SYSTEM_FEE
        assert deal.yahoo_shipping == 500
        assert deal.amazon_fee == 1500  # 10% of 15000

    def test_size_based_forwarding(self):
        """When package dimensions are available, use size-based cost."""
        # 3-side total = 200 + 300 + 250 = 750mm → 80 size = 840 yen
        product = self._product_with_used(
            10000,
            packageHeight=200, packageLength=300, packageWidth=250,
        )
        deal = score_deal(
            yahoo_price=3000,
            keepa_product=product,
            forwarding_cost=960,  # fallback, should NOT be used
        )
        assert deal is not None
        assert deal.forwarding_cost == 840  # size-based, not fallback
        assert deal.total_cost == 3000 + 0 + 840 + SYSTEM_FEE

    def test_over_200_size_excluded(self):
        """Package > 200 size (3-side total > 2000mm) → None."""
        product = self._product_with_used(
            10000,
            packageHeight=800, packageLength=800, packageWidth=500,
        )
        deal = score_deal(yahoo_price=3000, keepa_product=product)
        assert deal is None

    def test_no_dimensions_uses_fallback(self):
        """No package dimensions → use forwarding_cost fallback."""
        deal = score_deal(
            yahoo_price=3000,
            keepa_product=self._product_with_used(10000),
            forwarding_cost=960,
        )
        assert deal is not None
        assert deal.forwarding_cost == 960
        assert deal.total_cost == 3000 + 0 + 960 + SYSTEM_FEE

    def test_sells_well_flag(self):
        """sells_well is True when rank <= threshold."""
        deal = score_deal(
            yahoo_price=1000,
            keepa_product=self._product_with_used(5000, rank=50000),
            good_rank_threshold=100000,
        )
        assert deal is not None
        assert deal.sells_well is True

    def test_sells_poorly_flag(self):
        """sells_well is False when rank > threshold."""
        deal = score_deal(
            yahoo_price=1000,
            keepa_product=self._product_with_used(5000, rank=200000),
            good_rank_threshold=100000,
        )
        assert deal is not None
        assert deal.sells_well is False

    def test_new_only_product_excluded(self):
        """Products with only new price (no used price) are excluded."""
        product = _make_product(
            current=[-1, 8000, -1, 50000],
            avg30=[-1, 8000, -1, 50000],
            avg90=[-1, 8000, -1, 50000],
        )
        deal = score_deal(yahoo_price=3000, keepa_product=product)
        assert deal is None

    def test_used_price_used_as_sell_price(self):
        """Sell price is based on used price, not new price."""
        product = _make_product(
            current=[-1, 15000, 8000, 50000],
            avg30=[-1, 15000, 8000, 50000],
            avg90=[-1, 15000, 8000, 50000],
        )
        deal = score_deal(yahoo_price=3000, keepa_product=product)
        assert deal is not None
        assert deal.sell_price == 8000
