"""Strategy 7: Suggest Cross-Match keyword discovery.

Combines Amazon autocomplete suggestions with Yahoo Auction search results
to find model numbers with confirmed demand (Amazon) AND supply (Yahoo).

Flow:
  1. Build seed brand list from past insights + curated defaults
  2. For each seed brand:
     a. Fetch Amazon.co.jp autocomplete suggestions
     b. Search Yahoo Auctions and extract model numbers from listing titles
  3. Cross-match: model numbers found on BOTH platforms → high confidence
  4. Amazon-only model numbers → medium confidence (demand exists)
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import settings
from ..matcher import extract_model_numbers_from_text, is_apparel
from .analyzer import KeywordInsights
from .generator import CandidateProposal

logger = logging.getLogger(__name__)

# Amazon.co.jp autocomplete API
AMAZON_SUGGEST_URL = "https://completion.amazon.co.jp/api/2017/suggestions"
AMAZON_MARKETPLACE_ID = "A1VC38T7YXB528"  # Japan

# Default seed brands for cold-start (high-value categories for reselling)
_DEFAULT_SEEDS: list[str] = [
    # Gaming
    "Nintendo Switch",
    "PlayStation",
    # Audio
    "SONY WH",
    "SONY WF",
    "Bose",
    "JBL",
    # Camera
    "Canon",
    "Nikon",
    "FUJIFILM",
    # Electronics
    "Dyson",
    "Panasonic",
    "Pioneer",
    # Collectibles
    "GoPro",
    "SEGA",
    "Bandai",
]

# Max seeds to query per cycle
_MAX_SEEDS_PER_CYCLE = 10

# Max suggestions to keep per seed
_MAX_SUGGESTIONS_PER_SEED = 10

# Delay between seed queries (seconds)
_INTER_SEED_DELAY = 0.5


async def _fetch_amazon_suggestions(
    query: str,
    client: httpx.AsyncClient,
) -> list[str]:
    """Fetch autocomplete suggestions from Amazon.co.jp."""
    try:
        resp = await client.get(
            AMAZON_SUGGEST_URL,
            params={
                "mid": AMAZON_MARKETPLACE_ID,
                "alias": "aps",
                "prefix": query,
            },
        )
        if resp.status_code != 200:
            logger.debug("Amazon suggest %d for '%s'", resp.status_code, query)
            return []

        data = resp.json()
        suggestions: list[str] = []
        for s in data.get("suggestions", []):
            value = s.get("value", "").strip()
            if value and value.lower() != query.lower():
                suggestions.append(value)
        return suggestions[:_MAX_SUGGESTIONS_PER_SEED]

    except Exception as e:
        logger.debug("Amazon suggest error for '%s': %s", query, e)
        return []


async def _fetch_yahoo_models(
    query: str,
    scraper,
) -> set[str]:
    """Search Yahoo Auctions and extract model numbers from listing titles.

    Uses the existing YahooAuctionScraper.search() which returns parsed
    SearchResultItem objects with title fields.
    """
    try:
        results = await scraper.search(query, page=1)
        if not results:
            return set()

        models: set[str] = set()
        for item in results:
            extracted = extract_model_numbers_from_text(item.title)
            models.update(extracted)
        return models

    except Exception as e:
        logger.debug("Yahoo model extraction error for '%s': %s", query, e)
        return set()


def _build_seed_list(insights: KeywordInsights | None) -> list[str]:
    """Build seed brand list from insights (high performers) + curated defaults."""
    seeds: list[str] = []

    # Prioritize brands with proven profitability
    if insights and insights.brand_patterns:
        for bp in insights.brand_patterns[:10]:
            if bp.deal_count >= 2 and bp.avg_profit >= 2000:
                seeds.append(bp.brand_name)

    # Fill with curated defaults (avoid duplicates)
    seen = {s.lower() for s in seeds}
    for brand in _DEFAULT_SEEDS:
        if brand.lower() not in seen:
            seeds.append(brand)
            seen.add(brand.lower())

    return seeds[:_MAX_SEEDS_PER_CYCLE]


async def generate_suggest_crossmatch(
    scraper,
    existing: set[str],
    insights: KeywordInsights | None = None,
    max_count: int = 15,
) -> list[CandidateProposal]:
    """Strategy 7: Cross-match Amazon suggestions with Yahoo Auction listings.

    Returns CandidateProposal list (never raises, returns [] on error).

    Cross-matched models (both platforms) get confidence 0.75.
    Amazon-only models get confidence 0.60.
    """
    if not settings.suggest_crossmatch_enabled:
        return []

    seeds = _build_seed_list(insights)
    if not seeds:
        return []

    candidates: list[CandidateProposal] = []

    async with httpx.AsyncClient(
        timeout=15.0,
        headers={
            "User-Agent": settings.scraper_user_agent,
            "Accept-Language": "ja-JP,ja;q=0.9",
        },
    ) as amazon_client:
        for brand in seeds:
            if len(candidates) >= max_count:
                break

            # Fetch Amazon suggestions and Yahoo listings concurrently
            amazon_suggestions, yahoo_models = await asyncio.gather(
                _fetch_amazon_suggestions(brand, amazon_client),
                _fetch_yahoo_models(brand, scraper),
            )

            # Extract model numbers from Amazon suggestions
            # Map: model_number -> original suggestion text (for reasoning)
            amazon_models: dict[str, str] = {}
            for suggestion in amazon_suggestions:
                models = extract_model_numbers_from_text(suggestion)
                for model in models:
                    if model not in amazon_models:
                        amazon_models[model] = suggestion

            if not amazon_models:
                await asyncio.sleep(_INTER_SEED_DELAY)
                continue

            # Cross-match: models found on BOTH platforms
            cross_matched = set(amazon_models.keys()) & yahoo_models
            amazon_only = set(amazon_models.keys()) - yahoo_models

            # Cross-matched → high confidence (demand + supply confirmed)
            for model in sorted(cross_matched):
                keyword = f"{brand} {model}"
                if keyword.lower() in existing or is_apparel(keyword):
                    continue
                candidates.append(CandidateProposal(
                    keyword=keyword,
                    strategy="suggest",
                    confidence=0.75,
                    parent_keyword_id=None,
                    reasoning=(
                        f"Amazon検索サジェスト＋Yahoo出品の両方で確認: "
                        f"「{amazon_models[model]}」"
                    ),
                ))
                if len(candidates) >= max_count:
                    break

            # Amazon-only → medium confidence (demand exists, supply unconfirmed)
            for model in sorted(amazon_only):
                if len(candidates) >= max_count:
                    break
                keyword = f"{brand} {model}"
                if keyword.lower() in existing or is_apparel(keyword):
                    continue
                candidates.append(CandidateProposal(
                    keyword=keyword,
                    strategy="suggest",
                    confidence=0.60,
                    parent_keyword_id=None,
                    reasoning=f"Amazonサジェストで検出: 「{amazon_models[model]}」",
                ))

            await asyncio.sleep(_INTER_SEED_DELAY)

    logger.info(
        "Suggest cross-match: %d candidates from %d seeds",
        len(candidates), len(seeds),
    )
    return candidates
