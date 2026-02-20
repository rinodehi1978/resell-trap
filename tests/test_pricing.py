"""Tests for Amazon pricing calculations."""

import pytest

from yafuama.amazon.pricing import calculate_amazon_price, generate_sku


class TestCalculateAmazonPrice:
    def test_basic_calculation(self):
        # cost = 3000 + 800 = 3800, divisor = 1 - 25/100 = 0.75
        # raw = 3800 / 0.75 = 5066.67 -> ceil to 5070
        price = calculate_amazon_price(3000, 800, margin_pct=15.0, amazon_fee_pct=10.0)
        assert price == 5070

    def test_rounds_up_to_ten(self):
        # cost = 1000 + 0 = 1000, divisor = 0.75
        # raw = 1333.33 -> ceil to 1340
        price = calculate_amazon_price(1000, 0, margin_pct=15.0, amazon_fee_pct=10.0)
        assert price == 1340

    def test_exact_multiple_of_ten(self):
        # cost = 750 + 0 = 750, divisor = 0.75
        # raw = 1000.0 -> ceil = 1000 (exact)
        price = calculate_amazon_price(750, 0, margin_pct=15.0, amazon_fee_pct=10.0)
        assert price == 1000

    def test_zero_price_returns_zero(self):
        assert calculate_amazon_price(0, 800) == 0

    def test_negative_price_returns_zero(self):
        assert calculate_amazon_price(-100, 800) == 0

    def test_high_margin(self):
        price = calculate_amazon_price(5000, 1000, margin_pct=30.0, amazon_fee_pct=10.0)
        # divisor = 0.6, raw = 10000
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
        price = calculate_amazon_price(1000, 2000, margin_pct=15.0, amazon_fee_pct=10.0)
        # cost = 3000, divisor = 0.75, raw = 4000
        assert price == 4000


class TestGenerateSku:
    def test_basic(self):
        assert generate_sku("1219987808") == "YAHOO-1219987808"

    def test_alphanumeric(self):
        assert generate_sku("x1219674283") == "YAHOO-x1219674283"
