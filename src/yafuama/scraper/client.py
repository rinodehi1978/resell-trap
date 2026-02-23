"""HTTP client for fetching Yahoo! Auctions pages."""

from __future__ import annotations

import logging

import httpx

from ..config import settings

logger = logging.getLogger(__name__)


class AuctionGoneError(Exception):
    """Raised when a Yahoo auction page returns 404/410 (removed or expired)."""

    def __init__(self, url: str, status_code: int) -> None:
        self.url = url
        self.status_code = status_code
        super().__init__(f"Auction gone (HTTP {status_code}): {url}")

YAHOO_AUCTION_ITEM_URL = "https://auctions.yahoo.co.jp/jp/auction/{}"
YAHOO_SEARCH_URL = "https://auctions.yahoo.co.jp/search/search"

_HEADERS = {
    "User-Agent": settings.scraper_user_agent,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
}


class YahooClient:
    """Async HTTP client with optional Selenium fallback."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=_HEADERS,
                timeout=settings.scraper_request_timeout,
                follow_redirects=True,
            )
        return self._client

    async def fetch_auction_page(self, auction_id: str) -> str | None:
        url = YAHOO_AUCTION_ITEM_URL.format(auction_id)
        return await self._fetch(url)

    async def fetch_search_page(self, query: str, page: int = 1) -> str | None:
        params = {"p": query, "b": str((page - 1) * 50 + 1), "n": "50"}
        return await self._fetch(YAHOO_SEARCH_URL, params=params)

    async def _fetch(self, url: str, params: dict | None = None) -> str | None:
        """Fetch a URL. Returns HTML string, None on error, or raises AuctionGoneError on 404."""
        client = await self._get_client()
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            logger.warning("HTTP %s for %s", status_code, url)
            if status_code in (404, 410):
                raise AuctionGoneError(url, status_code) from e
            if settings.scraper_use_selenium_fallback:
                return self._selenium_fallback(url)
            return None
        except httpx.RequestError as e:
            logger.warning("Request error for %s: %s", url, e)
            if settings.scraper_use_selenium_fallback:
                return self._selenium_fallback(url)
            return None

    @staticmethod
    def _selenium_fallback(url: str) -> str | None:
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
            from webdriver_manager.chrome import ChromeDriverManager

            options = Options()
            options.add_argument("--headless")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            driver = webdriver.Chrome(
                service=Service(ChromeDriverManager().install()), options=options
            )
            try:
                driver.get(url)
                return driver.page_source
            finally:
                driver.quit()
        except ImportError:
            logger.warning("Selenium not installed; cannot use fallback")
            return None
        except Exception as e:
            logger.warning("Selenium fallback failed: %s", e)
            return None

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
