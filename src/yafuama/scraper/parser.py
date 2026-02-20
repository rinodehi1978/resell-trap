"""Parsers for Yahoo! Auctions HTML pages.

AuctionPageParser  – individual auction page (uses var pageData JSON)
SearchResultsParser – search results page (uses BS4 DOM parsing)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone, timedelta

from bs4 import BeautifulSoup, Tag

from ..schemas import AuctionData, SearchResultItem

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
YAHOO_AUCTION_URL = "https://auctions.yahoo.co.jp/jp/auction/{}"


class AuctionPageParser:
    """Parse an individual Yahoo! Auctions product page."""

    _PAGE_DATA_RE = re.compile(r"var\s+pageData\s*=\s*(\{.*?\})\s*;")
    _OG_IMAGE_RE = re.compile(r'<meta\s+property="og:image"\s+content="([^"]+)"')
    _DESCRIPTION_RE = re.compile(
        r'<meta\s+(?:property="og:description"|name="description")\s+content="([^"]+)"'
    )
    _SELLER_RE = re.compile(r"/seller/([^\"\'&?\s]+)")
    _IMG_URL_RE = re.compile(
        r'https://auctions\.c\.yimg\.jp/images\.auctions\.yahoo\.co\.jp/image/[^\s"\'<>]+'
    )

    def parse(self, html: str) -> AuctionData | None:
        m = self._PAGE_DATA_RE.search(html)
        if not m:
            logger.warning("pageData not found in HTML")
            return None

        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse pageData JSON: %s", e)
            return None

        items = data.get("items", {})
        auction_id = items.get("productID", "")
        if not auction_id:
            return None

        # Parse times – pageData uses "YYYY-MM-DD HH:MM:SS" in JST
        start_time = self._parse_datetime(items.get("starttime"))
        end_time = self._parse_datetime(items.get("endtime"))

        # Extract image from og:image meta tag
        image_url = ""
        img_match = self._OG_IMAGE_RE.search(html)
        if img_match:
            image_url = img_match.group(1)

        # Extract seller_id from page content
        seller_id = ""
        seller_match = self._SELLER_RE.search(html)
        if seller_match:
            seller_id = seller_match.group(1)

        return AuctionData(
            auction_id=auction_id,
            title=items.get("productName", ""),
            url=YAHOO_AUCTION_URL.format(auction_id),
            image_url=image_url,
            category_id=items.get("productCategoryID", ""),
            seller_id=seller_id,
            current_price=int(items.get("price", 0)),
            start_price=int(items.get("price", 0)),  # pageData doesn't expose startPrice separately
            buy_now_price=0,  # not in pageData
            win_price=int(items.get("winPrice", 0)),
            start_time=start_time,
            end_time=end_time,
            bid_count=int(items.get("bids", 0)),
            is_closed=items.get("isClosed") == "1",
            has_winner=items.get("hasWinner") == "1",
        )

    @staticmethod
    def _parse_datetime(s: str | None) -> datetime | None:
        if not s:
            return None
        try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=JST)
        except ValueError:
            return None

    def extract_all_images(self, html: str) -> list[str]:
        """Extract all product image URLs from an auction page.

        Tries (in order): pageData JSON fields, og:image meta, DOM regex fallback.
        """
        images: list[str] = []

        # 1. pageData JSON
        m = self._PAGE_DATA_RE.search(html)
        if m:
            try:
                data = json.loads(m.group(1))
                items = data.get("items", {})
                for key in ("imageUrls", "images", "img"):
                    val = items.get(key)
                    if isinstance(val, list):
                        for v in val:
                            url = v if isinstance(v, str) else (v.get("url", "") if isinstance(v, dict) else "")
                            if url:
                                images.append(url)
                    elif isinstance(val, str) and val:
                        images.append(val)
                    if images:
                        break
            except (json.JSONDecodeError, TypeError):
                pass

        # 2. og:image fallback (single image)
        if not images:
            og = self._OG_IMAGE_RE.search(html)
            if og:
                images.append(og.group(1))

        # 3. DOM regex fallback — find all Yahoo auction CDN image URLs
        if not images:
            images = self._IMG_URL_RE.findall(html)

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for url in images:
            if url and url not in seen:
                seen.add(url)
                unique.append(url)
        return unique

    def extract_description(self, html: str) -> str:
        """Extract product description text from a Yahoo auction page.

        Uses meta description / og:description tags (~200 chars of seller text).
        """
        m = self._DESCRIPTION_RE.search(html)
        return m.group(1) if m else ""

class SearchResultsParser:
    """Parse Yahoo! Auctions search results page."""

    def parse(self, html: str) -> list[SearchResultItem]:
        soup = BeautifulSoup(html, "lxml")
        results: list[SearchResultItem] = []

        for li in soup.select("li.Product"):
            try:
                item = self._parse_product(li)
                if item:
                    results.append(item)
            except Exception as e:
                logger.warning("Failed to parse search result item: %s", e)
                continue

        return results

    def _parse_product(self, li: Tag) -> SearchResultItem | None:
        # The data attributes are spread across multiple child elements.
        # Collect all data-auction-* attributes from the entire <li>.
        attrs: dict[str, str] = {}
        for el in li.find_all(attrs={"data-auction-id": True}):
            for key, val in el.attrs.items():
                if key.startswith("data-auction-") and val:
                    attr_name = key.replace("data-auction-", "")
                    if attr_name not in attrs or not attrs[attr_name]:
                        attrs[attr_name] = val

        auction_id = attrs.get("id", "")
        if not auction_id:
            return None

        # End time is a unix timestamp
        end_time = None
        raw_end = attrs.get("endtime", "")
        if raw_end:
            try:
                end_time = datetime.fromtimestamp(int(raw_end), tz=JST)
            except (ValueError, OSError):
                pass

        # Bid count from .Product__bid
        bid_count = 0
        bid_el = li.select_one(".Product__bid")
        if bid_el:
            try:
                bid_count = int(bid_el.get_text(strip=True))
            except ValueError:
                pass

        # Shipping cost from DOM text (e.g. "送料無料", "送料 ¥1,000")
        shipping_cost = self._parse_shipping(li)

        # Buy-now price: first try data attribute, then parse from DOM text "即決"
        buy_now_price = int(attrs.get("buynowprice", 0))
        if buy_now_price <= 0:
            buy_now_price = self._parse_buy_now_price(li)

        return SearchResultItem(
            auction_id=auction_id,
            title=attrs.get("title", ""),
            url=YAHOO_AUCTION_URL.format(auction_id),
            image_url=attrs.get("img", ""),
            current_price=int(attrs.get("price", 0)),
            buy_now_price=buy_now_price,
            start_price=int(attrs.get("startprice", 0)),
            bid_count=bid_count,
            end_time=end_time,
            seller_id=attrs.get("auc-seller-id", ""),
            category_id=attrs.get("category", ""),
            shipping_cost=shipping_cost,
        )

    _PRICE_DIGITS_RE = re.compile(r"[\d,]+")

    def _parse_buy_now_price(self, li: Tag) -> int:
        """Extract buy-now (即決) price from Product__price DOM elements.

        Yahoo shows:
          <div class="Product__price">
            <span class="Product__label">即決</span>
            <span class="Product__priceValue">3,950円</span>
          </div>
        """
        for price_div in li.select(".Product__price"):
            label_el = price_div.select_one(".Product__label")
            if not label_el:
                continue
            label = label_el.get_text(strip=True)
            if "即決" not in label:
                continue
            value_el = price_div.select_one(".Product__priceValue")
            if value_el:
                text = value_el.get_text(strip=True).replace(",", "")
                m = self._PRICE_DIGITS_RE.search(text)
                if m:
                    try:
                        return int(m.group(0))
                    except ValueError:
                        pass
        return 0

    def _parse_shipping(self, li: Tag) -> int | None:
        """Extract shipping cost from search result item.

        Yahoo Auctions shows shipping as text like "送料無料" (free) or
        "送料 ¥1,000". Returns None if not found, 0 for free shipping.
        """
        # Try common shipping label selectors
        for selector in (".Product__shipping", ".Product__postage",
                         "[class*='shipping']", "[class*='postage']"):
            el = li.select_one(selector)
            if el:
                text = el.get_text(strip=True)
                if "無料" in text or "free" in text.lower():
                    return 0
                m = self._PRICE_DIGITS_RE.search(text.replace(",", ""))
                if m:
                    try:
                        return int(m.group(0))
                    except ValueError:
                        pass
                return None

        # Fallback: scan all text for shipping pattern
        full_text = li.get_text()
        if "送料無料" in full_text:
            return 0

        return None
