"""Price calculation for Amazon listings."""

from __future__ import annotations

import math


def calculate_amazon_price(
    estimated_win_price: int,
    shipping_cost: int,
    forwarding_cost: int = 0,
    system_fee: int = 100,
    margin_pct: float = 15.0,
    amazon_fee_pct: float = 10.0,
) -> int:
    """Calculate Amazon listing price.

    Formula: price = (win_price + shipping + forwarding + system_fee) / (1 - (margin + fee) / 100)
    Rounded up to nearest 10 JPY.
    """
    if estimated_win_price <= 0:
        return 0
    total_cost = estimated_win_price + shipping_cost + forwarding_cost + system_fee
    divisor = 1.0 - (margin_pct + amazon_fee_pct) / 100.0
    if divisor <= 0:
        raise ValueError("Combined margin and fees exceed 100%")
    raw = total_cost / divisor
    return int(math.ceil(raw / 10) * 10)


def generate_sku(auction_id: str) -> str:
    """Auto-generate an Amazon SKU from a Yahoo Auction ID."""
    return f"YAHOO-{auction_id}"
