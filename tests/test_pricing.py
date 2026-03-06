"""Tests for Amazon pricing calculations."""

import pytest

from yafuama.amazon.pricing import calculate_amazon_price, generate_sku


class TestCalculateAmazonPrice:
    def test_basic_calculation(self):
        # cost = 3000 + 800 + 960 + 100 = 4860, divisor = 1 - 25/100 = 0.75
        # raw = 4860 / 0.75 = 6480
        price = calculate_amazon_price(3000, 800, forwarding_cost=960, margin_pct=15.0, amazon_fee_pct=10.0)
        assert price == 6480

    def test_without_forwarding(self):
        # cost = 3000 + 800 + 0 + 100 = 3900, divisor = 0.75
        # raw = 3900 / 0.75 = 5200
        price = calculate_amazon_price(3000, 800, forwarding_cost=0, system_fee=100, margin_pct=15.0, amazon_fee_pct=10.0)
        assert price == 5200

    def test_without_system_fee(self):
        # cost = 3000 + 800 + 0 + 0 = 3800, divisor = 0.75
        # raw = 3800 / 0.75 = 5066.67 -> 5070
        price = calculate_amazon_price(3000, 800, system_fee=0, margin_pct=15.0, amazon_fee_pct=10.0)
        assert price == 5070

    def test_rounds_up_to_ten(self):
        # cost = 1000 + 0 + 0 + 100 = 1100, divisor = 0.75
        # raw = 1466.67 -> 1470
        price = calculate_amazon_price(1000, 0, margin_pct=15.0, amazon_fee_pct=10.0)
        assert price == 1470

    def test_exact_multiple_of_ten(self):
        # cost = 650 + 0 + 0 + 100 = 750, divisor = 0.75
        # raw = 1000.0 -> exact
        price = calculate_amazon_price(650, 0, margin_pct=15.0, amazon_fee_pct=10.0)
        assert price == 1000

    def test_zero_price_returns_zero(self):
        assert calculate_amazon_price(0, 800) == 0

    def test_negative_price_returns_zero(self):
        assert calculate_amazon_price(-100, 800) == 0

    def test_high_margin(self):
        # cost = 5000 + 1000 + 0 + 0 = 6000, divisor = 0.6, raw = 10000
        price = calculate_amazon_price(5000, 1000, forwarding_cost=0, system_fee=0, margin_pct=30.0, amazon_fee_pct=10.0)
        assert price == 10000

    def test_fees_exceed_100_raises(self):
        with pytest.raises(ValueError, match="exceed 100%"):
            calculate_amazon_price(1000, 0, margin_pct=50.0, amazon_fee_pct=51.0)

    def test_fees_equal_100_raises(self):
        with pytest.raises(ValueError, match="exceed 100%"):
            calculate_amazon_price(1000, 0, margin_pct=50.0, amazon_fee_pct=50.0)

    def test_zero_shipping(self):
        price = calculate_amazon_price(2000, 0, margin_pct=15.0, amazon_fee_pct=10.0)
        assert price > 2000

    def test_large_shipping(self):
        # cost = 1000 + 2000 + 0 + 0 = 3000, divisor = 0.75, raw = 4000
        price = calculate_amazon_price(1000, 2000, forwarding_cost=0, system_fee=0, margin_pct=15.0, amazon_fee_pct=10.0)
        assert price == 4000

    def test_yamazen_tv_scenario(self):
        """山善テレビの実例で検証: 転送料+システム料込みでマージンが正確になる"""
        # Yahoo即決¥17,250 + 送料¥0 + 転送料¥1,340 + システム¥100 = ¥18,690
        # マージン26.9% + 手数料10% = 36.9%
        # divisor = 1 - 0.369 = 0.631
        # raw = 18690 / 0.631 = 29619.6 → 29620
        price = calculate_amazon_price(
            17250, 0, forwarding_cost=1340, system_fee=100,
            margin_pct=26.9, amazon_fee_pct=10.0,
        )
        assert price == 29620
        # Verify actual margin
        amazon_fee = int(price * 10.0 / 100)  # ¥2,962
        total_cost = 17250 + 0 + 1340 + 100 + amazon_fee  # ¥21,652
        profit = price - total_cost  # ¥7,968
        margin = profit / price * 100  # 26.9%
        assert round(margin, 1) == 26.9

    def test_forwarding_cost_default(self):
        """forwarding_cost defaults to 0, system_fee defaults to 100"""
        price_default = calculate_amazon_price(10000, 0, margin_pct=15.0, amazon_fee_pct=10.0)
        price_explicit = calculate_amazon_price(10000, 0, forwarding_cost=0, system_fee=100, margin_pct=15.0, amazon_fee_pct=10.0)
        assert price_default == price_explicit


class TestGenerateSku:
    def test_basic(self):
        assert generate_sku("1219987808") == "YAHOO-1219987808"

    def test_alphanumeric(self):
        assert generate_sku("x1219674283") == "YAHOO-x1219674283"
