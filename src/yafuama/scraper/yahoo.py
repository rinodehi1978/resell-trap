"""Yahoo! Auctions scraping orchestrator."""

from __future__ import annotations

import logging
import re

from ..schemas import AuctionData, SearchResultItem
from .client import AuctionGoneError, YahooClient
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
        try:
            html = await self.client.fetch_auction_page(auction_id)
        except AuctionGoneError:
            logger.info("Auction %s removed (404/410) — treating as ended", auction_id)
            return AuctionData(
                auction_id=auction_id,
                is_closed=True,
                has_winner=False,
            )
        if not html:
            return None
        return self._page_parser.parse(html)

    async def fetch_auction_images(self, auction_id: str) -> list[str]:
        """Fetch all product image URLs from a Yahoo auction page."""
        html = await self.client.fetch_auction_page(auction_id)
        if not html:
            return []
        return self._page_parser.extract_all_images(html)

    async def fetch_auction_description(self, auction_id: str) -> str:
        """Fetch the product description from a Yahoo auction page.

        This is an expensive operation (1 HTTP request per call).
        Only use for secondary validation of high-value candidates.
        """
        html = await self.client.fetch_auction_page(auction_id)
        if not html:
            return ""
        return self._page_parser.extract_description(html)

    async def search(self, query: str, page: int = 1) -> list[SearchResultItem]:
        html = await self.client.fetch_search_page(query, page)
        if not html:
            return []
        return self._search_parser.parse(html)

    async def close(self) -> None:
        await self.client.close()
