"""Validates keyword candidates by checking Yahoo/Keepa for actual price gaps."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ..config import settings
from ..keepa.analyzer import score_deal
from .generator import CandidateProposal

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    is_valid: bool
    yahoo_result_count: int = 0
    keepa_result_count: int = 0
    potential_deals: int = 0
    best_margin: float = 0.0
    best_profit: int = 0
    keepa_tokens_used: int = 0
    rejection_reason: str = ""

    def to_json(self) -> str:
        return json.dumps({
            "yahoo_count": self.yahoo_result_count,
            "keepa_count": self.keepa_result_count,
            "deals": self.potential_deals,
            "best_margin": self.best_margin,
            "best_profit": self.best_profit,
        })


async def validate_candidate(
    candidate: CandidateProposal,
    scraper,
    keepa_client,
    token_budget: int,
) -> ValidationResult:
    """Validate a keyword candidate against Yahoo + Keepa data.

    Steps:
    1. Search Yahoo (free) - reject if < 3 results
    2. Check token budget
    3. Search Keepa (1 token) - reject if 0 results
    4. Run score_deal() on top matches - reject if no deals pass threshold
    """
    # Step 1: Yahoo search (free)
    try:
        yahoo_results = await scraper.search(candidate.keyword, page=1)
    except Exception as e:
        logger.warning("Yahoo search failed for '%s': %s", candidate.keyword, e)
        return ValidationResult(
            is_valid=False,
            rejection_reason=f"Yahoo search error: {e}",
        )

    yahoo_count = len(yahoo_results) if yahoo_results else 0
    if yahoo_count < 3:
        return ValidationResult(
            is_valid=False,
            yahoo_result_count=yahoo_count,
            rejection_reason=f"Yahoo results too few ({yahoo_count} < 3)",
        )

    # Step 2: Token budget check
    if token_budget <= 0:
        return ValidationResult(
            is_valid=False,
            yahoo_result_count=yahoo_count,
            rejection_reason="Keepa token budget exhausted (deferred)",
        )

    # Step 3: Keepa search (costs 1 token)
    try:
        keepa_products = await keepa_client.search_products(
            candidate.keyword, stats=settings.keepa_default_stats_days
        )
    except Exception as e:
        logger.warning("Keepa search failed for '%s': %s", candidate.keyword, e)
        return ValidationResult(
            is_valid=False,
            yahoo_result_count=yahoo_count,
            keepa_tokens_used=1,
            rejection_reason=f"Keepa search error: {e}",
        )

    keepa_count = len(keepa_products) if keepa_products else 0
    if keepa_count == 0:
        return ValidationResult(
            is_valid=False,
            yahoo_result_count=yahoo_count,
            keepa_result_count=0,
            keepa_tokens_used=1,
            rejection_reason="No Keepa results (no Amazon demand)",
        )

    # Step 4: Quick deal scoring (top 5 Yahoo x top 5 Keepa)
    deals_found = 0
    best_margin = 0.0
    best_profit = 0

    top_yahoo = yahoo_results[:5]
    top_keepa = keepa_products[:5]

    for yr in top_yahoo:
        yahoo_price = yr.current_price if hasattr(yr, "current_price") else yr.get("current_price", 0)
        if yahoo_price <= 0:
            continue
        yahoo_shipping = (yr.shipping_cost if hasattr(yr, "shipping_cost") else yr.get("shipping_cost")) or 0

        for kp in top_keepa:
            deal = score_deal(
                yahoo_price=yahoo_price,
                keepa_product=kp,
                yahoo_shipping=yahoo_shipping,
                forwarding_cost=settings.deal_forwarding_cost,
                inspection_fee=settings.deal_inspection_fee,
                amazon_fee_pct=settings.deal_amazon_fee_pct,
                good_rank_threshold=settings.keepa_good_rank_threshold,
            )
            if deal and deal.gross_margin_pct >= settings.deal_min_gross_margin_pct \
                    and deal.gross_profit >= settings.deal_min_gross_profit:
                deals_found += 1
                if deal.gross_profit > best_profit:
                    best_profit = deal.gross_profit
                    best_margin = deal.gross_margin_pct

    if deals_found == 0:
        return ValidationResult(
            is_valid=False,
            yahoo_result_count=yahoo_count,
            keepa_result_count=keepa_count,
            keepa_tokens_used=1,
            rejection_reason="No profitable deals found in top matches",
        )

    return ValidationResult(
        is_valid=True,
        yahoo_result_count=yahoo_count,
        keepa_result_count=keepa_count,
        potential_deals=deals_found,
        best_margin=best_margin,
        best_profit=best_profit,
        keepa_tokens_used=1,
    )
