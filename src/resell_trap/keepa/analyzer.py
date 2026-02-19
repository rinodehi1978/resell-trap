"""Sales rank analysis and price recommendation from Keepa data.

All functions are pure: they take Keepa product data dicts and return
analysis results. No API calls, no side effects.

Keepa stats format:
  product["stats"]["current"][IDX]  = current value
  product["stats"]["avg30"][IDX]    = 30-day average
  product["stats"]["avg90"][IDX]    = 90-day average
  product["stats"]["min"][IDX]      = [min_value, keepa_time]
  product["stats"]["max"][IDX]      = [max_value, keepa_time]

CSV type indices: 0=AMAZON, 1=NEW, 2=USED, 3=SALES_RANK
Value -1 = no data available.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# CSV type indices in Keepa stats arrays
IDX_AMAZON = 0
IDX_NEW = 1
IDX_USED = 2
IDX_SALES_RANK = 3

DEFAULT_GOOD_RANK_THRESHOLD = 100_000


def _stat_val(stats: dict, key: str, idx: int) -> int | None:
    """Extract a scalar value from stats, returning None for -1 or missing."""
    arr = stats.get(key)
    if arr is None or idx >= len(arr):
        return None
    val = arr[idx]
    if val is None or val == -1:
        return None
    return int(val)


def _stat_minmax(stats: dict, key: str, idx: int) -> int | None:
    """Extract min or max value from stats.

    Keepa stores these as [keepa_time, value] pairs.
    Use 'minInInterval'/'maxInInterval' for the stats period.
    Fallback to 'min'/'max' (all-time) with [keepa_time, value] format.
    """
    arr = stats.get(key)
    if arr is None or idx >= len(arr):
        return None
    entry = arr[idx]
    if entry is None:
        return None
    # Pairs are [keepa_time, value] — value is at index 1
    if isinstance(entry, list) and len(entry) >= 2:
        val = entry[1]
    elif isinstance(entry, list) and len(entry) == 1:
        val = entry[0]
    else:
        val = entry
    if val is None or val == -1:
        return None
    return int(val)


# --- Data classes ---


@dataclass
class SalesRankAnalysis:
    """Sales rank trend analysis result."""
    current_rank: int | None
    avg_rank_30d: int | None
    avg_rank_90d: int | None
    min_rank_90d: int | None
    max_rank_90d: int | None
    rank_trend: str  # "improving" | "declining" | "stable" | "unknown"
    sells_well: bool
    rank_threshold_used: int


@dataclass
class UsedPriceAnalysis:
    """Used price history analysis result."""
    current_price: int | None
    avg_price_30d: int | None
    avg_price_90d: int | None
    min_price_90d: int | None
    max_price_90d: int | None
    price_trend: str  # "rising" | "falling" | "stable" | "unknown"
    price_volatility: float


@dataclass
class PriceRecommendation:
    """Recommended listing price based on Keepa analysis."""
    recommended_price: int
    strategy: str  # "competitive" | "undercut" | "market_average" | "margin_based"
    reasoning: str
    confidence: str  # "high" | "medium" | "low"
    market_price_avg: int | None
    market_price_min: int | None


@dataclass
class KeepaAnalysisResult:
    """Combined analysis result for an ASIN."""
    asin: str
    title: str
    sales_rank: SalesRankAnalysis
    used_price: UsedPriceAnalysis
    recommendation: PriceRecommendation | None


# --- Analysis functions ---


def analyze_sales_rank(
    product: dict[str, Any],
    good_rank_threshold: int = DEFAULT_GOOD_RANK_THRESHOLD,
) -> SalesRankAnalysis:
    """Analyze sales rank from Keepa stats to determine sales velocity."""
    stats = product.get("stats") or {}
    current = _stat_val(stats, "current", IDX_SALES_RANK)
    avg_30d = _stat_val(stats, "avg30", IDX_SALES_RANK)
    avg_90d = _stat_val(stats, "avg90", IDX_SALES_RANK)
    min_90d = _stat_minmax(stats, "minInInterval", IDX_SALES_RANK)
    max_90d = _stat_minmax(stats, "maxInInterval", IDX_SALES_RANK)

    # Determine trend: compare 30-day avg to 90-day avg
    # Lower rank = better (more sales)
    if avg_30d is not None and avg_90d is not None and avg_90d > 0:
        ratio = avg_30d / avg_90d
        if ratio < 0.85:
            trend = "improving"
        elif ratio > 1.15:
            trend = "declining"
        else:
            trend = "stable"
    else:
        trend = "unknown"

    sells_well = current is not None and current <= good_rank_threshold

    return SalesRankAnalysis(
        current_rank=current,
        avg_rank_30d=avg_30d,
        avg_rank_90d=avg_90d,
        min_rank_90d=min_90d,
        max_rank_90d=max_90d,
        rank_trend=trend,
        sells_well=sells_well,
        rank_threshold_used=good_rank_threshold,
    )


def analyze_used_price(product: dict[str, Any]) -> UsedPriceAnalysis:
    """Analyze used price from Keepa stats for market rate understanding."""
    stats = product.get("stats") or {}
    current = _stat_val(stats, "current", IDX_USED)
    avg_30d = _stat_val(stats, "avg30", IDX_USED)
    avg_90d = _stat_val(stats, "avg90", IDX_USED)
    min_90d = _stat_minmax(stats, "minInInterval", IDX_USED)
    max_90d = _stat_minmax(stats, "maxInInterval", IDX_USED)

    # Trend: compare 30-day avg to 90-day avg
    if avg_30d is not None and avg_90d is not None and avg_90d > 0:
        ratio = avg_30d / avg_90d
        if ratio > 1.10:
            trend = "rising"
        elif ratio < 0.90:
            trend = "falling"
        else:
            trend = "stable"
    else:
        trend = "unknown"

    # Volatility approximation from min/max range vs avg
    if min_90d is not None and max_90d is not None and avg_90d is not None and avg_90d > 0:
        volatility = round((max_90d - min_90d) / avg_90d, 3)
    else:
        volatility = 0.0

    return UsedPriceAnalysis(
        current_price=current,
        avg_price_30d=avg_30d,
        avg_price_90d=avg_90d,
        min_price_90d=min_90d,
        max_price_90d=max_90d,
        price_trend=trend,
        price_volatility=volatility,
    )


def recommend_price(
    sales_rank: SalesRankAnalysis,
    used_price: UsedPriceAnalysis,
    cost_price: int,
    shipping_cost: int = 800,
    margin_pct: float = 15.0,
    amazon_fee_pct: float = 10.0,
) -> PriceRecommendation:
    """Generate a recommended listing price based on Keepa analysis.

    Strategy:
    1. sells_well + market data -> "undercut" (97% of current market, above floor)
    2. poor sales + market data -> "market_average" (90-day avg, no rush)
    3. no market data -> "margin_based" (same formula as amazon/pricing.py)
    """
    # Floor price: same formula as calculate_amazon_price()
    if cost_price <= 0:
        floor_price = 0
    else:
        total_cost = cost_price + shipping_cost
        divisor = 1.0 - (margin_pct + amazon_fee_pct) / 100.0
        if divisor <= 0:
            floor_price = total_cost * 3
        else:
            floor_price = int(math.ceil(total_cost / divisor / 10) * 10)

    # No market data -> margin-based fallback
    if used_price.current_price is None or used_price.avg_price_90d is None:
        return PriceRecommendation(
            recommended_price=floor_price,
            strategy="margin_based",
            reasoning="中古価格データなし。マージン計算ベースの価格を使用。",
            confidence="low",
            market_price_avg=None,
            market_price_min=None,
        )

    market_avg = used_price.avg_price_90d
    market_current = used_price.current_price
    market_min = used_price.min_price_90d

    # Good sales -> competitive pricing
    if sales_rank.sells_well:
        target = int(math.ceil(market_current * 0.97 / 10) * 10)
        recommended = max(target, floor_price)

        if recommended < market_current:
            strategy = "undercut"
            reasoning = (
                f"売れ行き良好（ランク{sales_rank.current_rank:,}）。"
                f"現在の中古相場¥{market_current:,}より若干安く設定。"
            )
        else:
            strategy = "competitive"
            reasoning = (
                f"売れ行き良好（ランク{sales_rank.current_rank:,}）。"
                f"原価を考慮し相場付近で設定。"
            )

        return PriceRecommendation(
            recommended_price=recommended,
            strategy=strategy,
            reasoning=reasoning,
            confidence="high" if used_price.price_volatility < 0.5 else "medium",
            market_price_avg=market_avg,
            market_price_min=market_min,
        )

    # Poor sales -> market average, no rush
    target = int(math.ceil(market_avg / 10) * 10)
    recommended = max(target, floor_price)

    return PriceRecommendation(
        recommended_price=recommended,
        strategy="market_average",
        reasoning=(
            f"売れ行き低調（ランク{sales_rank.current_rank:,}）。"
            f"90日平均相場¥{market_avg:,}付近で設定。急ぐ必要なし。"
        ),
        confidence="medium" if used_price.price_volatility < 0.5 else "low",
        market_price_avg=market_avg,
        market_price_min=market_min,
    )


def analyze_product(
    product: dict[str, Any],
    cost_price: int = 0,
    shipping_cost: int = 800,
    margin_pct: float = 15.0,
    amazon_fee_pct: float = 10.0,
    good_rank_threshold: int = DEFAULT_GOOD_RANK_THRESHOLD,
) -> KeepaAnalysisResult:
    """Run full analysis on a Keepa product data dict.

    Main entry point for the analyzer module.
    """
    asin = product.get("asin") or ""
    title = product.get("title") or ""

    sales_rank = analyze_sales_rank(product, good_rank_threshold)
    used_price_analysis = analyze_used_price(product)

    recommendation = None
    if cost_price > 0:
        recommendation = recommend_price(
            sales_rank, used_price_analysis,
            cost_price=cost_price,
            shipping_cost=shipping_cost,
            margin_pct=margin_pct,
            amazon_fee_pct=amazon_fee_pct,
        )

    return KeepaAnalysisResult(
        asin=asin,
        title=title,
        sales_rank=sales_rank,
        used_price=used_price_analysis,
        recommendation=recommendation,
    )


# --- Deal scoring ---


@dataclass
class DealCandidate:
    """A potential arbitrage deal: Yahoo item matched with Amazon product."""
    yahoo_title: str
    yahoo_price: int
    yahoo_auction_id: str
    yahoo_url: str
    yahoo_image_url: str
    amazon_asin: str
    amazon_title: str
    amazon_used_price: int | None
    amazon_new_price: int | None
    sales_rank: int | None
    sells_well: bool
    estimated_profit: int
    profit_margin_pct: float
    recommended_sell_price: int
    rank_trend: str
    price_trend: str


def score_deal(
    yahoo_price: int,
    keepa_product: dict[str, Any],
    shipping_cost: int = 800,
    margin_pct: float = 15.0,
    amazon_fee_pct: float = 10.0,
    good_rank_threshold: int = DEFAULT_GOOD_RANK_THRESHOLD,
) -> DealCandidate | None:
    """Score a potential deal by comparing Yahoo price to Amazon market data.

    Returns None if no usable price data is available from Keepa.
    """
    asin = keepa_product.get("asin") or ""
    title = keepa_product.get("title") or ""

    stats = keepa_product.get("stats") or {}
    used_price = _stat_val(stats, "current", IDX_USED)
    new_price = _stat_val(stats, "current", IDX_NEW)
    rank = _stat_val(stats, "current", IDX_SALES_RANK)

    # We need at least one sell price
    sell_price = used_price or new_price
    if sell_price is None or sell_price <= 0:
        return None

    # Calculate costs
    total_cost = yahoo_price + shipping_cost
    fee_pct = (margin_pct + amazon_fee_pct) / 100.0
    if fee_pct >= 1.0:
        return None

    # Floor price (break-even with margin)
    floor_price = int(math.ceil(total_cost / (1.0 - fee_pct) / 10) * 10)

    # Recommended sell price: use used_price if available, else new_price
    recommended = max(sell_price, floor_price)

    # Profit = sell price - cost - fees
    amazon_fees = int(recommended * amazon_fee_pct / 100)
    profit = recommended - total_cost - amazon_fees
    profit_pct = (profit / total_cost * 100) if total_cost > 0 else 0

    # Trends
    sr = analyze_sales_rank(keepa_product, good_rank_threshold)
    up = analyze_used_price(keepa_product)

    return DealCandidate(
        yahoo_title="",  # filled by caller
        yahoo_price=yahoo_price,
        yahoo_auction_id="",  # filled by caller
        yahoo_url="",
        yahoo_image_url="",
        amazon_asin=asin,
        amazon_title=title,
        amazon_used_price=used_price,
        amazon_new_price=new_price,
        sales_rank=rank,
        sells_well=sr.sells_well,
        estimated_profit=profit,
        profit_margin_pct=round(profit_pct, 1),
        recommended_sell_price=recommended,
        rank_trend=sr.rank_trend,
        price_trend=up.price_trend,
    )
