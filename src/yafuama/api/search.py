"""Yahoo Auctions search pass-through endpoint."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from ..schemas import SearchResponse
from ..scraper.yahoo import YahooAuctionScraper

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["search"])


def _get_scraper() -> YahooAuctionScraper:
    from ..main import app_state
    return app_state["scraper"]


@router.get("/search", response_model=SearchResponse)
async def search_yahoo(
    q: str = Query(..., min_length=1, description="Search query"),
    page: int = Query(1, ge=1, description="Page number"),
):
    scraper = _get_scraper()
    try:
        items = await scraper.search(q, page)
    except Exception as e:
        logger.warning("Yahoo search failed for '%s': %s", q, e)
        raise HTTPException(502, f"Yahoo search failed: {e}")
    return SearchResponse(query=q, page=page, items=items)
