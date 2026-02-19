"""Keepa API endpoints: product analysis and price recommendations."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..keepa import KeepaApiError
from ..keepa.analyzer import analyze_product
from ..schemas import (
    KeepaAnalysisRequest,
    KeepaAnalysisResponse,
    PriceRecommendationResponse,
    SalesRankAnalysisResponse,
    UsedPriceAnalysisResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/keepa", tags=["keepa"])


def _get_keepa_client():
    from ..main import app_state

    client = app_state.get("keepa")
    if client is None:
        raise HTTPException(503, "Keepa API is not configured")
    return client


@router.get("/analysis/{asin}", response_model=KeepaAnalysisResponse)
async def get_analysis(
    asin: str,
    cost_price: int = 0,
    shipping_cost: int = 0,
    margin_pct: float | None = None,
    good_rank_threshold: int | None = None,
):
    """Fetch Keepa data for an ASIN and return sales/price analysis."""
    client = _get_keepa_client()

    effective_margin = margin_pct if margin_pct is not None else settings.sp_api_default_margin_pct
    effective_threshold = good_rank_threshold or settings.keepa_good_rank_threshold
    effective_shipping = shipping_cost or settings.sp_api_default_shipping_cost

    try:
        product = await client.query_product(asin, stats=settings.keepa_default_stats_days)
    except KeepaApiError as e:
        raise HTTPException(502, f"Keepa API error: {e}") from e

    result = analyze_product(
        product,
        cost_price=cost_price,
        shipping_cost=effective_shipping,
        margin_pct=effective_margin,
        good_rank_threshold=effective_threshold,
    )

    return KeepaAnalysisResponse(
        asin=result.asin,
        title=result.title,
        sales_rank=SalesRankAnalysisResponse(
            current_rank=result.sales_rank.current_rank,
            avg_rank_30d=result.sales_rank.avg_rank_30d,
            avg_rank_90d=result.sales_rank.avg_rank_90d,
            min_rank_90d=result.sales_rank.min_rank_90d,
            max_rank_90d=result.sales_rank.max_rank_90d,
            rank_trend=result.sales_rank.rank_trend,
            sells_well=result.sales_rank.sells_well,
            rank_threshold_used=result.sales_rank.rank_threshold_used,
        ),
        used_price=UsedPriceAnalysisResponse(
            current_price=result.used_price.current_price,
            avg_price_30d=result.used_price.avg_price_30d,
            avg_price_90d=result.used_price.avg_price_90d,
            min_price_90d=result.used_price.min_price_90d,
            max_price_90d=result.used_price.max_price_90d,
            price_trend=result.used_price.price_trend,
            price_volatility=result.used_price.price_volatility,
        ),
        recommendation=PriceRecommendationResponse(
            recommended_price=result.recommendation.recommended_price,
            strategy=result.recommendation.strategy,
            reasoning=result.recommendation.reasoning,
            confidence=result.recommendation.confidence,
            market_price_avg=result.recommendation.market_price_avg,
            market_price_min=result.recommendation.market_price_min,
        ) if result.recommendation else None,
    )


@router.post("/analysis", response_model=KeepaAnalysisResponse)
async def post_analysis(body: KeepaAnalysisRequest):
    """Same as GET but accepts a JSON body for structured cost parameters."""
    return await get_analysis(
        asin=body.asin,
        cost_price=body.cost_price,
        shipping_cost=body.shipping_cost,
        margin_pct=body.margin_pct,
        good_rank_threshold=body.good_rank_threshold,
    )
