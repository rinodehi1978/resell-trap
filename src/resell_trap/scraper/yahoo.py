"""Yahoo! Auctions scraping orchestrator."""

from __future__ import annotations

import logging
import re

from ..schemas import AuctionData, SearchResultItem
from .client import YahooClient
from .parser import AuctionPageParser, SearchResultsParser

logger = logging.getLogger(__name__)

_AUCTION_ID_RE = re.compile(r"([a-zA-Z]?\d{7,})")


def extract_auction_id(input_str: str) -> str | None:
    """Extract auction_id from a URL or raw ID string."""
    m = _AUCTION_ID_RE.search(input_str)
    return m.group(1) if m else None


class YahooAuctionScraper:
    """High-level scraping orchestrator."""

    def __init__(self) -> None:
        self.client = YahooClient()
        self._page_parser = AuctionPageParser()
        self._search_parser = SearchResultsParser()

    async def fetch_auction(self, auction_id: str) -> AuctionData | None:
        html = await self.client.fetch_auction_page(auction_id)
        if not html:
            return None
        return self._page_parser.parse(html)

    async def search(self, query: str, page: int = 1) -> list[SearchResultItem]:
        html = await self.client.fetch_search_page(query, page)
        if not html:
            return []
        return self._search_parser.parse(html)

    async def close(self) -> None:
        await self.client.close()
