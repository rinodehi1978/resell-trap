"""Automated deal scanner: Product Finder → model extraction → Yahoo search → matching → notification."""

from __future__ import annotations

import asyncio
import logging
import re
from time import monotonic
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..database import SessionLocal
from ..keepa.analyzer import score_deal
from ..matcher import (
    extract_accessory_signals_from_text,
    extract_model_numbers_from_text,
    is_apparel,
    is_valid_model,
)
from ..models import DealAlert
from ..notifier.webhook import LINE_NOTIFY_URL, send_webhook

logger = logging.getLogger(__name__)

YAHOO_AUCTION_URL = "https://auctions.yahoo.co.jp/jp/auction/{}"

_BARCODE_RE = re.compile(r"^\d{8,}$")


def _is_barcode(text: str) -> bool:
    """Detect barcodes/EAN codes masquerading as model numbers."""
    return bool(_BARCODE_RE.match(text.strip()))


class DealScanner:
    """Scans Product Finder products against Yahoo Auctions for profitable deals."""

    # Amazon.co.jp ルートカテゴリ（無在庫転売に適したカテゴリをローテーション）
    _PF_CATEGORIES = [
        (3210981, "家電＆カメラ"),
        (2016929051, "DIY・工具・ガーデン"),
        (13299531, "おもちゃ"),
        (2277721051, "ホビー"),
        (2123629051, "楽器・音響機器"),
        (3828871, "ホーム＆キッチン"),
        (14304371, "スポーツ＆アウトドア"),
        (2127209051, "パソコン・周辺機器"),
        (86731051, "文房具・オフィス用品"),
        (2277724051, "大型家電"),
    ]

    def __init__(
        self,
        scraper,
        keepa_client,
        webhook_url: str = "",
        webhook_type: str = "discord",
        sp_api_client=None,
    ) -> None:
        self._scraper = scraper
        self._keepa = keepa_client
        self._webhook_url = webhook_url
        self._webhook_type = webhook_type
        self._sp_api = sp_api_client
        self._pf_cache: tuple[float, list[dict]] | None = None
        self._category_index: int = 0  # カテゴリローテーション用

    # ── Helper methods ─────────────────────────────────────────────────

    _JAPANESE_RE = re.compile(r"[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]")

    @staticmethod
    def _normalize_model(model: str) -> str:
        """Normalize model number for comparison: lowercase + remove hyphens."""
        return re.sub(r"[-\u30fc]", "", model.lower())

    @staticmethod
    def _is_book_asin(asin: str) -> bool:
        """Check if ASIN is likely a book (ISBN-10: all digits, or ISBN-13: starts with 978/979)."""
        if not asin:
            return False
        if asin.isdigit():
            return True
        if len(asin) == 13 and asin.startswith(("978", "979")) and asin[3:].isdigit():
            return True
        return False

    @staticmethod
    def _is_valid_model(s: str) -> bool:
        """Delegate to shared is_valid_model in matcher module."""
        return is_valid_model(s)

    def _extract_yahoo_keywords(self, keepa_product: dict) -> list[str]:
        """Extract Yahoo search keywords from a Keepa product.

        Priority:
        1. product["model"] field (if not a barcode and looks like a model number)
        2. Model numbers from title (fallback)

        Rules:
        - No model number → empty list (exclude)
        - Model ≤4 chars → exclude
        - Model ≥5 chars → use as keyword
        - Must contain ASCII letter + digit, no Japanese chars
        - Max 3 keywords per product
        """
        models: list[str] = []

        # Try model field first
        model_field = (keepa_product.get("model") or "").strip()
        if (model_field and not _is_barcode(model_field)
                and len(model_field) >= 5 and self._is_valid_model(model_field)):
            models.append(model_field)

        # Fallback: extract from title
        if not models:
            title = keepa_product.get("title") or ""
            title_models = extract_model_numbers_from_text(title)
            models = [m for m in title_models
                      if len(m) >= 5 and self._is_valid_model(m)]

        # Deduplicate by normalized form
        seen: set[str] = set()
        unique: list[str] = []
        for m in models:
            norm = self._normalize_model(m)
            if norm not in seen:
                seen.add(norm)
                unique.append(m)

        return unique[:3]

    @staticmethod
    def _is_auction_ended(yr) -> bool:
        """Check if a Yahoo auction has already ended based on end_time."""
        end_time = yr.end_time if hasattr(yr, "end_time") else yr.get("end_time")
        if end_time is None:
            return False  # Unknown end_time — don't filter
        now = datetime.now(timezone.utc)
        return end_time <= now

    async def _match_yahoo_to_amazon(self, yr, keepa_product: dict):
        """Match a Yahoo item to an Amazon/Keepa product using model number matching.

        Simple exact-match after hyphen removal:
        - Extract models from both Yahoo title and Keepa product
        - Normalize (lowercase + remove hyphens)
        - Exact match only (SV18 vs SV18FF = different products)
        - Exclude apparel, junk, accessories

        Returns a scored DealCandidate or None.
        """
        yahoo_title = yr.title if hasattr(yr, "title") else yr.get("title", "")
        buy_now = yr.buy_now_price if hasattr(yr, "buy_now_price") else yr.get("buy_now_price", 0)

        if buy_now <= 0:
            return None

        # Skip ended auctions
        if self._is_auction_ended(yr):
            return None

        # Exclude apparel and junk
        if is_apparel(yahoo_title):
            return None
        if "ジャンク" in yahoo_title:
            return None

        # Accessory check
        if extract_accessory_signals_from_text(yahoo_title):
            return None

        # Extract Amazon model numbers (normalized, 5+ chars)
        amazon_models: set[str] = set()
        model_field = (keepa_product.get("model") or "").strip()
        if model_field and not _is_barcode(model_field) and len(model_field) >= 5:
            amazon_models.add(self._normalize_model(model_field))
        amazon_title = keepa_product.get("title") or ""
        for m in extract_model_numbers_from_text(amazon_title):
            if len(m) >= 5:
                amazon_models.add(self._normalize_model(m))

        if not amazon_models:
            return None

        # Extract Yahoo model numbers (normalized, 5+ chars)
        yahoo_models: set[str] = set()
        for m in extract_model_numbers_from_text(yahoo_title):
            if len(m) >= 5:
                yahoo_models.add(self._normalize_model(m))

        # Exact match (after normalization)
        if not amazon_models & yahoo_models:
            return None

        # Score the deal
        yr_shipping = yr.shipping_cost if hasattr(yr, "shipping_cost") else yr.get("shipping_cost")

        # Dynamic referral fee lookup via SP-API
        fee_pct = settings.deal_amazon_fee_pct
        if self._sp_api is not None:
            _asin = keepa_product.get("asin", "")
            _stats = keepa_product.get("stats") or {}
            _current = _stats.get("current") or []
            _used_price = _current[2] if len(_current) > 2 and _current[2] not in (None, -1) else 0
            if _asin and _used_price > 0:
                actual_pct = await self._sp_api.get_referral_fee_pct(_asin, _used_price)
                if actual_pct is not None:
                    fee_pct = actual_pct

        deal = score_deal(
            yahoo_price=buy_now,
            keepa_product=keepa_product,
            yahoo_shipping=yr_shipping,
            forwarding_cost=settings.deal_forwarding_cost,
            amazon_fee_pct=fee_pct,
            good_rank_threshold=settings.keepa_good_rank_threshold,
        )

        if not deal:
            return None

        # Price ratio sanity check
        if deal.sell_price > 0 and buy_now < deal.sell_price * 0.25:
            return None

        # Populate Yahoo fields
        deal.yahoo_title = yahoo_title
        deal.yahoo_price = buy_now
        if yr_shipping is not None:
            deal.yahoo_shipping = yr_shipping
        deal.yahoo_auction_id = yr.auction_id if hasattr(yr, "auction_id") else yr.get("auction_id", "")
        deal.yahoo_url = yr.url if hasattr(yr, "url") else yr.get("url", "")
        deal.yahoo_image_url = yr.image_url if hasattr(yr, "image_url") else yr.get("image_url", "")

        return deal

    # ── Product Finder pipeline ────────────────────────────────────────

    async def _get_pf_products(self) -> list[dict]:
        """Fetch products from Keepa Product Finder with category rotation."""
        now = monotonic()

        # Return cached if still valid
        if self._pf_cache is not None:
            cached_at, products = self._pf_cache
            if now - cached_at < settings.pf_cache_ttl:
                logger.info("Product Finder: using cache (%d products)", len(products))
                return products

        # Check token budget
        tokens = self._keepa.tokens_left
        if tokens is not None and tokens <= 100:
            logger.warning("Keepa tokens low (%s), skipping Product Finder", tokens)
            return []

        # カテゴリローテーション: 毎回違うカテゴリを検索
        cat_id, cat_name = self._PF_CATEGORIES[self._category_index % len(self._PF_CATEGORIES)]
        self._category_index += 1

        try:
            products = await self._keepa.product_finder(
                selection={
                    "rootCategory": cat_id,
                    "salesRankDrops90_gte": settings.demand_finder_min_drops90,
                    "current_USED_gte": settings.demand_finder_min_used_price,
                    "perPage": settings.demand_finder_max_results,
                },
                stats=settings.keepa_default_stats_days,
            )
        except Exception as e:
            logger.warning("Product Finder failed (%s): %s", cat_name, e)
            return []

        if not products:
            logger.info("Product Finder: 0 products in %s", cat_name)
            return []

        # Filter: exclude products where used >= new (overpriced used)
        filtered = []
        for p in products:
            stats = p.get("stats") or {}
            current = stats.get("current") or []
            used = current[2] if len(current) > 2 and current[2] not in (None, -1) else 0
            new = current[1] if len(current) > 1 and current[1] not in (None, -1) else 0
            if new > 0 and used >= new:
                continue
            filtered.append(p)

        self._pf_cache = (now, filtered)
        logger.info("Product Finder [%s]: %d products (%d after filter)", cat_name, len(products), len(filtered))
        return filtered

    async def _scan_pf_deals(self, products: list[dict], db) -> int:
        """Scan Product Finder products against Yahoo Auctions."""
        yahoo_searches = 0
        total_deals = 0

        for product in products:
            keywords = self._extract_yahoo_keywords(product)
            if not keywords:
                continue

            for keyword in keywords:
                if yahoo_searches >= settings.pf_max_yahoo_searches:
                    logger.info("PF scan: Yahoo search limit reached (%d)", yahoo_searches)
                    return total_deals

                # Search Yahoo
                yahoo_results = []
                for page in range(1, settings.deal_scan_max_pages + 1):
                    try:
                        page_results = await self._scraper.search(keyword, page=page)
                    except Exception as e:
                        logger.warning("Yahoo search failed for PF keyword '%s': %s", keyword, e)
                        break
                    if not page_results:
                        break
                    yahoo_results.extend(page_results)
                yahoo_searches += 1

                if not yahoo_results:
                    await asyncio.sleep(0.3)
                    continue

                # Match each Yahoo result against this Amazon product
                deals = []
                for yr in yahoo_results:
                    deal = await self._match_yahoo_to_amazon(yr, product)
                    if deal and (
                        deal.gross_margin_pct >= settings.deal_min_gross_margin_pct
                        and deal.gross_margin_pct <= settings.deal_max_gross_margin_pct
                        and deal.gross_profit >= settings.deal_min_gross_profit
                    ):
                        deals.append(deal)

                if not deals:
                    await asyncio.sleep(0.3)
                    continue

                # Process deals (best first)
                deals.sort(key=lambda d: d.gross_profit, reverse=True)
                for deal in deals:
                    result = await self._process_deal(deal, keyword, db)
                    if result:
                        total_deals += 1

                await asyncio.sleep(0.3)

        return total_deals

    async def _process_deal(self, deal, keyword: str, db) -> dict | None:
        """Process a matched deal: dedup check, save alert, notify.

        Returns a deal summary dict or None if skipped.
        """
        # Reject book ASINs (ISBN format: all digits)
        if self._is_book_asin(deal.amazon_asin):
            logger.debug("Skipping book ASIN: %s (%s)", deal.amazon_asin, deal.yahoo_title[:50])
            return None

        # Check if already notified (exact auction+ASIN match)
        existing = (
            db.query(DealAlert)
            .filter(
                DealAlert.yahoo_auction_id == deal.yahoo_auction_id,
                DealAlert.amazon_asin == deal.amazon_asin,
            )
            .first()
        )
        if existing:
            return None

        # ASIN dedup: skip if same ASIN notified recently (different auction)
        dedup_cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.deal_dedup_hours)
        recent_same_asin = (
            db.query(DealAlert)
            .filter(
                DealAlert.amazon_asin == deal.amazon_asin,
                DealAlert.status.in_(["active", "listed"]),
                DealAlert.notified_at >= dedup_cutoff,
            )
            .first()
        )
        if recent_same_asin:
            # Allow if this deal is significantly better (profit improved by ¥1000+)
            if deal.gross_profit <= recent_same_asin.gross_profit + 1000:
                logger.debug(
                    "ASIN dedup: skipping %s (ASIN %s, recent alert %d hrs ago)",
                    deal.yahoo_auction_id, deal.amazon_asin, settings.deal_dedup_hours,
                )
                return None

        # Verify auction is still live before notifying
        try:
            auction_data = await self._scraper.fetch_auction(deal.yahoo_auction_id)
            if auction_data is None:
                logger.info("Auction %s unreachable — skipping deal", deal.yahoo_auction_id)
                return None
            if getattr(auction_data, "is_closed", False):
                logger.info("Auction %s already ended — skipping deal", deal.yahoo_auction_id)
                return None
        except Exception:
            pass  # Network error — don't block the deal, let it through

        # Record alert BEFORE webhook (crash-safe: prevents duplicate notifications)
        alert = DealAlert(
            search_keyword=keyword,
            yahoo_auction_id=deal.yahoo_auction_id,
            amazon_asin=deal.amazon_asin,
            yahoo_title=deal.yahoo_title,
            yahoo_url=deal.yahoo_url,
            yahoo_image_url=deal.yahoo_image_url or "",
            amazon_title=deal.amazon_title or "",
            yahoo_price=deal.yahoo_price,
            yahoo_shipping=deal.yahoo_shipping,
            sell_price=deal.sell_price,
            gross_profit=deal.gross_profit,
            gross_margin_pct=deal.gross_margin_pct,
            amazon_fee_pct=round(deal.amazon_fee / deal.sell_price * 100, 1) if deal.sell_price else 10.0,
            forwarding_cost=deal.forwarding_cost,
        )
        try:
            nested = db.begin_nested()
            db.add(alert)
            db.flush()
        except IntegrityError:
            nested.rollback()
            logger.debug("Duplicate alert skipped: %s + %s", deal.yahoo_auction_id, deal.amazon_asin)
            return None

        # Send notification (after DB save to prevent duplicates on crash)
        await self._send_webhook(deal, keyword)

        return {
            "yahoo_title": deal.yahoo_title,
            "yahoo_price": deal.yahoo_price,
            "sell_price": deal.sell_price,
            "gross_profit": deal.gross_profit,
            "gross_margin_pct": deal.gross_margin_pct,
        }

    # ── Main scan loop ────────────────────────────────────────────────

    async def scan_all(self) -> None:
        """Product Finder → model extraction → Yahoo search → matching → notification."""
        self._keepa.clear_search_cache()
        if self._sp_api is not None:
            self._sp_api.reset_fee_quota()
        db = SessionLocal()
        try:
            pf_deals = 0
            products = []
            if settings.keepa_enabled:
                products = await self._get_pf_products()
                if products:
                    pf_deals = await self._scan_pf_deals(products, db)
                    db.commit()
            logger.info(
                "Scan complete: %d deals from %d products",
                pf_deals, len(products),
            )
        except Exception as e:
            logger.exception("Error in deal scan: %s", e)
            db.rollback()
        finally:
            db.close()

    # ── Webhook ────────────────────────────────────────────────────────

    async def _send_webhook(self, deal, keyword: str) -> None:
        """Send a deal notification via webhook."""
        if not self._webhook_url:
            logger.info(
                "Deal found (no webhook): %s ¥%s → ¥%s profit ¥%s (%.1f%%)",
                deal.yahoo_title, deal.yahoo_price, deal.sell_price,
                deal.gross_profit, deal.gross_margin_pct,
            )
            return

        yahoo_url = deal.yahoo_url or YAHOO_AUCTION_URL.format(deal.yahoo_auction_id)
        amazon_url = f"https://amazon.co.jp/dp/{deal.amazon_asin}"

        if self._webhook_type == "discord":
            payload = {
                "embeds": [{
                    "title": f"Deal: {deal.yahoo_title[:100]}",
                    "url": yahoo_url,
                    "color": 0x00C853,  # Green
                    "fields": [
                        {
                            "name": "Yahoo",
                            "value": f"¥{deal.yahoo_price:,}" + (
                                " (送料無料)" if deal.yahoo_shipping == 0
                                else " (送料不明)" if deal.yahoo_shipping is None
                                else f" (+送料¥{deal.yahoo_shipping:,})"
                            ),
                            "inline": True,
                        },
                        {
                            "name": "Amazon中古",
                            "value": f"¥{deal.sell_price:,}",
                            "inline": True,
                        },
                        {
                            "name": "粗利",
                            "value": f"¥{deal.gross_profit:,} ({deal.gross_margin_pct}%)",
                            "inline": True,
                        },
                        {
                            "name": "ランク",
                            "value": f"{deal.sales_rank:,}" if deal.sales_rank else "-",
                            "inline": True,
                        },
                        {
                            "name": "リンク",
                            "value": f"[Yahoo]({yahoo_url}) | [Amazon]({amazon_url})",
                            "inline": False,
                        },
                    ],
                    "footer": {"text": f"キーワード: {keyword}"},
                    "thumbnail": {"url": deal.yahoo_image_url} if deal.yahoo_image_url else {},
                }],
            }
        elif self._webhook_type == "slack":
            msg = (
                f"*Deal:* {deal.yahoo_title}\n"
                f"Yahoo ¥{deal.yahoo_price:,} → Amazon中古 ¥{deal.sell_price:,}\n"
                f"粗利 ¥{deal.gross_profit:,} ({deal.gross_margin_pct}%)\n"
                f"<{yahoo_url}|Yahoo> | <{amazon_url}|Amazon>\n"
                f"キーワード: {keyword}"
            )
            payload = {"text": msg}
        elif self._webhook_type == "line":
            msg = (
                f"\nDeal: {deal.yahoo_title}\n"
                f"Yahoo ¥{deal.yahoo_price:,} → Amazon中古 ¥{deal.sell_price:,}\n"
                f"粗利 ¥{deal.gross_profit:,} ({deal.gross_margin_pct}%)\n"
                f"Yahoo: {yahoo_url}\nAmazon: {amazon_url}\n"
                f"キーワード: {keyword}"
            )
            payload = {"message": msg, "token": self._webhook_url}
        else:
            msg = (
                f"Deal: {deal.yahoo_title}\n"
                f"Yahoo ¥{deal.yahoo_price:,} → Amazon中古 ¥{deal.sell_price:,}\n"
                f"粗利 ¥{deal.gross_profit:,} ({deal.gross_margin_pct}%)\n"
                f"Yahoo: {yahoo_url}\nAmazon: {amazon_url}\n"
                f"キーワード: {keyword}"
            )
            payload = {"message": msg}

        url = LINE_NOTIFY_URL if self._webhook_type == "line" else self._webhook_url
        success = await send_webhook(url, payload, webhook_type=self._webhook_type)
        if not success:
            logger.warning("Deal webhook failed for: %s", deal.yahoo_title[:60])
