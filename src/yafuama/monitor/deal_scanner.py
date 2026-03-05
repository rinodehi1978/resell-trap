"""Automated deal scanner: periodically searches watched keywords for profitable deals."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from time import monotonic
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..database import SessionLocal
from ..keepa import KeepaApiError
from ..keepa.analyzer import score_deal
from ..matcher import (
    extract_accessory_signals_from_text,
    extract_model_numbers_from_text,
    extract_product_info,
    is_apparel,
    is_valid_model,
    match_products,
)
from ..ai.generator import _is_barcode
from ..models import DealAlert, WatchedKeyword
from ..notifier.webhook import LINE_NOTIFY_URL, send_webhook

logger = logging.getLogger(__name__)

YAHOO_AUCTION_URL = "https://auctions.yahoo.co.jp/jp/auction/{}"

# Patterns for filtering garbage Keepa search queries (40 tokens wasted per empty search)
_HEX_LIKE_RE = re.compile(r"^[0-9a-f]{6,}$", re.IGNORECASE)
_SERIAL_RE = re.compile(r"^\d{4,}[a-z]?$", re.IGNORECASE)  # 4+ digits with optional letter
_LONG_GARBAGE_RE = re.compile(r"^\w{16,}$")  # Very long single tokens (serial numbers)
_DIGIT_PREFIX_WORD_RE = re.compile(r"^\d{1,2}[a-z]{3,}$", re.IGNORECASE)  # "1chav" from "1ch AVアンプ"
_LOT_NUMBER_RE = re.compile(r"^[a-z]\d{7,}$", re.IGNORECASE)  # "n10902951", "t10873938"


class DealScanner:
    """Scans watched keywords for profitable Yahoo→Amazon deals and sends notifications."""

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

    # ── Helper methods for Product Finder pipeline ──────────────────────

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

    @staticmethod
    def _clean_keepa_query(query: str) -> str:
        """Remove garbage tokens from Keepa search queries.

        Strips serial numbers, hex strings, and other noise extracted from
        Yahoo titles that cause 0-result searches (40 tokens wasted each).
        Returns cleaned query, or empty string if nothing valid remains.
        """
        clean_tokens = []
        for token in query.split():
            # Too short to be meaningful
            if len(token) < 3:
                continue
            # Very long single token = serial number / lot code
            if _LONG_GARBAGE_RE.match(token):
                continue
            # Hex-like string (02257f, 1230b00ff, etc.)
            if _HEX_LIKE_RE.match(token):
                continue
            # Pure serial number: 4+ digits with optional trailing letter (1798f, 12345a)
            if _SERIAL_RE.match(token):
                continue
            # Digit-prefix word: "1chav" from "1ch AVアンプ" misparse
            if _DIGIT_PREFIX_WORD_RE.match(token):
                continue
            # Lot/serial number: single letter + 7+ digits (n10902951, t10873938)
            if _LOT_NUMBER_RE.match(token):
                continue
            clean_tokens.append(token)

        return " ".join(clean_tokens)

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

    # ── Product Finder pipeline (Phase 1) ─────────────────────────────

    async def _get_pf_products(self) -> list[dict]:
        """Fetch products from Keepa Product Finder with caching."""
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

        try:
            products = await self._keepa.product_finder(
                selection={
                    "salesRankDrops90_gte": settings.demand_finder_min_drops90,
                    "current_USED_gte": settings.demand_finder_min_used_price,
                    "perPage": settings.demand_finder_max_results,
                },
                stats=settings.keepa_default_stats_days,
            )
        except Exception as e:
            logger.warning("Product Finder failed: %s", e)
            return []

        if not products:
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
        logger.info("Product Finder: %d products (%d after filter)", len(products), len(filtered))
        return filtered

    async def _scan_pf_deals(self, products: list[dict], db) -> int:
        """Phase 1: Scan Product Finder products against Yahoo Auctions."""
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

                # Find or create WatchedKeyword for this search term
                kw = (
                    db.query(WatchedKeyword)
                    .filter(WatchedKeyword.keyword == keyword)
                    .first()
                )
                if not kw:
                    kw = WatchedKeyword(
                        keyword=keyword,
                        source="product_finder",
                        is_active=True,
                    )
                    db.add(kw)
                    db.flush()

                # Process deals (best first)
                deals.sort(key=lambda d: d.gross_profit, reverse=True)
                for deal in deals:
                    result = await self._process_deal(deal, kw, db)
                    if result:
                        total_deals += 1

                await asyncio.sleep(0.3)

        return total_deals

    async def _process_deal(self, deal, kw: WatchedKeyword, db) -> dict | None:
        """Process a matched deal: dedup check, save alert, notify, series expansion.

        Shared by both Phase 1 (Product Finder) and Phase 2 (manual keywords).
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
            keyword_id=kw.id,
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

        # Update keyword stats for learning loop
        kw.total_deals_found += 1
        kw.total_gross_profit += deal.gross_profit

        # Send notification (after DB save to prevent duplicates on crash)
        await self._send_webhook(deal, kw)

        return {
            "yahoo_title": deal.yahoo_title,
            "yahoo_price": deal.yahoo_price,
            "sell_price": deal.sell_price,
            "gross_profit": deal.gross_profit,
            "gross_margin_pct": deal.gross_margin_pct,
        }

    # ── Main scan loop ────────────────────────────────────────────────

    async def scan_all(self) -> None:
        """Scan for deals in two phases:

        Phase 1: Product Finder pipeline (Amazon-first, main source)
          - Keepa Product Finder → extract model numbers → Yahoo search → match → notify
        Phase 2: Keyword scan (model + generic)
          - Model keywords: Yahoo search → Keepa targeted match → score
          - Generic keywords: Keepa search → filter → extract models → Yahoo scan
        """
        self._keepa.clear_search_cache()
        if self._sp_api is not None:
            self._sp_api.reset_fee_quota()
        db = SessionLocal()
        try:
            # Phase 1: Product Finder pipeline (Amazon-first)
            pf_deals = 0
            if settings.keepa_enabled:
                try:
                    products = await self._get_pf_products()
                    if products:
                        pf_deals = await self._scan_pf_deals(products, db)
                        db.commit()
                        logger.info(
                            "Phase 1 (Product Finder): %d new deals from %d products",
                            pf_deals, len(products),
                        )
                except Exception as e:
                    logger.exception("Phase 1 (Product Finder) error: %s", e)
                    db.rollback()

            # Phase 2: Manual + AI-generated keywords
            keywords = (
                db.query(WatchedKeyword)
                .filter(
                    WatchedKeyword.is_active == True,  # noqa: E712
                    WatchedKeyword.source.in_(["manual", "ai_demand", "ai_series", "deal_extract"]),
                )
                .order_by(
                    WatchedKeyword.last_scanned_at.is_(None).desc(),
                    WatchedKeyword.last_scanned_at.asc(),
                )
                .all()
            )

            scanned = 0
            for kw in keywords:
                # Check Keepa token budget and throttle state before each keyword
                if self._keepa.is_throttled:
                    logger.info(
                        "Keepa throttled, pausing Phase 2 after %d/%d keywords.",
                        scanned, len(keywords),
                    )
                    break
                tokens = self._keepa.tokens_left
                if tokens is not None and tokens <= 5:
                    logger.info(
                        "Keepa tokens low (%s), pausing Phase 2 after %d/%d keywords.",
                        tokens, scanned, len(keywords),
                    )
                    break

                # Skip dormant keywords (5+ consecutive scans with no deals)
                dormant_threshold = 5
                if kw.scans_since_last_deal >= dormant_threshold and kw.total_scans >= dormant_threshold:
                    kw.last_scanned_at = datetime.now(timezone.utc)
                    kw.total_scans += 1
                    kw.scans_since_last_deal += 1
                    scanned += 1
                    continue

                try:
                    new_deals = await self._scan_keyword(kw, db)
                    kw.last_scanned_at = datetime.now(timezone.utc)
                    kw.total_scans += 1
                    if new_deals:
                        kw.scans_since_last_deal = 0
                    else:
                        kw.scans_since_last_deal += 1
                    scanned += 1
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.warning("Error scanning keyword '%s': %s", kw.keyword, e)

            # Auto-cleanup underperforming keywords
            self._cleanup_keywords(db)

            # Free ORM identity map to reduce memory
            db.expire_all()

            logger.info(
                "Scan cycle complete: PF=%d deals, Keywords=%d/%d scanned",
                pf_deals, scanned, len(keywords),
            )
            db.commit()
        except Exception as e:
            logger.exception("Error in deal scan loop: %s", e)
            db.rollback()
        finally:
            db.close()

    def _cleanup_keywords(self, db) -> None:
        """Auto-cleanup underperforming keywords after each scan cycle.

        執着しない。見つからないキーワードは早く切り捨てる。
        Rules:
        - AI/Product Finder生成 + 0 deals + 3+ scans → DEACTIVATE
        - Manual + 0 deals + 10+ scans → DEACTIVATE
        - Any source + has deals + 5+ consecutive scans without deal → DEACTIVATE
        """
        cleanup_threshold_manual = 10
        cleanup_threshold_ai = 3
        dormant_since_last_deal = 5

        keywords = db.query(WatchedKeyword).filter(WatchedKeyword.is_active == True).all()  # noqa: E712
        for kw in keywords:
            # AI/PF-generated: deactivate after 3 scans with no results
            if kw.source != "manual" and kw.total_deals_found == 0 and kw.total_scans >= cleanup_threshold_ai:
                logger.info(
                    "Auto-deactivating keyword '%s' (source=%s): %d scans, 0 deals",
                    kw.keyword, kw.source, kw.total_scans,
                )
                kw.is_active = False
                kw.auto_deactivated_at = datetime.now(timezone.utc)
                continue

            # Manual + never found a deal: deactivate after 10 scans
            if kw.source == "manual" and kw.total_deals_found == 0 and kw.total_scans >= cleanup_threshold_manual:
                logger.info(
                    "Auto-deactivating manual keyword '%s': %d scans, 0 deals",
                    kw.keyword, kw.total_scans,
                )
                kw.is_active = False
                kw.auto_deactivated_at = datetime.now(timezone.utc)
                continue

            # Any keyword with deals but gone dormant: deactivate after 5 consecutive dry scans
            if kw.total_deals_found > 0 and kw.scans_since_last_deal >= dormant_since_last_deal:
                logger.info(
                    "Auto-deactivating dormant keyword '%s': %d scans since last deal",
                    kw.keyword, kw.scans_since_last_deal,
                )
                kw.is_active = False
                kw.auto_deactivated_at = datetime.now(timezone.utc)

    async def scan_keyword_by_id(self, keyword_id: int) -> list[dict]:
        """Manually trigger scan for a single keyword. Returns list of new deal dicts."""
        db = SessionLocal()
        try:
            kw = db.query(WatchedKeyword).filter(WatchedKeyword.id == keyword_id).first()
            if not kw:
                return []
            new_deals = await self._scan_keyword(kw, db)
            kw.last_scanned_at = datetime.now(timezone.utc)
            kw.total_scans += 1
            if new_deals:
                kw.scans_since_last_deal = 0
            else:
                kw.scans_since_last_deal += 1
            db.commit()
            return new_deals
        except Exception as e:
            logger.exception("Error in manual scan for keyword %d: %s", keyword_id, e)
            db.rollback()
            return []
        finally:
            db.close()

    def _is_generic_keyword(self, keyword: str) -> bool:
        """Check if keyword is generic (no model number pattern).

        Generic keywords like "オーブンレンジ", "AVアンプ" should go Amazon-first.
        Model keywords like "wh1000xm4", "cfi2000a01" use normal Yahoo-first flow.
        """
        models = extract_model_numbers_from_text(keyword)
        valid_models = [m for m in models if len(m) >= 5 and is_valid_model(m)]
        return len(valid_models) == 0

    async def _scan_keyword(self, kw: WatchedKeyword, db) -> list[dict]:
        """Phase 2: Route to Amazon-first or Yahoo-first based on keyword type."""
        # Generic keywords → Amazon-first (Keepa search → filter → extract models → Yahoo)
        if self._is_generic_keyword(kw.keyword):
            return await self._scan_generic_keyword(kw, db)

        # Model keywords → existing Yahoo-first flow
        deals = await self._find_deals(kw.keyword)
        if not deals:
            return []

        new_deals = []
        for deal in deals:
            result = await self._process_deal(deal, kw, db)
            if result:
                new_deals.append(result)

        logger.info(
            "Keyword '%s': found %d deals, %d new",
            kw.keyword, len(deals), len(new_deals),
        )
        return new_deals

    async def _scan_generic_keyword(self, kw: WatchedKeyword, db) -> list[dict]:
        """Amazon-first scan for generic keywords.

        Flow: Keepa search → filter by criteria → extract models →
              register as WatchedKeywords + immediate Yahoo scan.

        Generic keywords (e.g. "オーブンレンジ") serve as Amazon discovery seeds.
        They find qualifying Amazon products, extract model numbers, and register
        those models for persistent monitoring.
        """
        # Step 1: Search Keepa with generic keyword (40 tokens)
        try:
            keepa_products = await self._keepa.search_products(
                kw.keyword, stats=settings.keepa_default_stats_days
            )
        except Exception as e:
            logger.warning("Generic keyword Keepa search failed for '%s': %s", kw.keyword, e)
            return []

        if not keepa_products:
            logger.info("Generic keyword '%s': no Keepa results", kw.keyword)
            return []

        # Step 2: Filter by user criteria (same as Product Finder)
        filtered = []
        for p in keepa_products:
            stats = p.get("stats") or {}
            current = stats.get("current") or []
            used = current[2] if len(current) > 2 and current[2] not in (None, -1) else 0
            new = current[1] if len(current) > 1 and current[1] not in (None, -1) else 0

            # Skip if used price below minimum
            if used < settings.demand_finder_min_used_price:
                continue
            # Skip if used >= new (overpriced used)
            if new > 0 and used >= new:
                continue

            filtered.append(p)

        logger.info(
            "Generic keyword '%s': %d Keepa results, %d after filter",
            kw.keyword, len(keepa_products), len(filtered),
        )

        if not filtered:
            return []

        # Step 3: Extract models and register as WatchedKeywords
        registered = 0
        for p in filtered:
            models = self._extract_yahoo_keywords(p)
            for model in models:
                existing = (
                    db.query(WatchedKeyword)
                    .filter(WatchedKeyword.keyword == model)
                    .first()
                )
                if existing:
                    continue
                new_kw = WatchedKeyword(
                    keyword=model,
                    is_active=True,
                    source="deal_extract",
                    parent_keyword_id=kw.id,
                    confidence=0.8,
                    notes=f"Extracted from Keepa: {p.get('asin', '')} via '{kw.keyword}'",
                )
                try:
                    nested = db.begin_nested()
                    db.add(new_kw)
                    db.flush()
                    registered += 1
                    logger.info(
                        "Auto-registered model '%s' from generic keyword '%s' (ASIN: %s)",
                        model, kw.keyword, p.get("asin", ""),
                    )
                except IntegrityError:
                    nested.rollback()

        if registered:
            logger.info(
                "Generic keyword '%s': registered %d new model keywords",
                kw.keyword, registered,
            )

        # Step 4: Immediate Yahoo scan with extracted models (like Phase 1)
        new_deals = []
        yahoo_searches = 0
        max_yahoo = settings.pf_max_yahoo_searches

        for product in filtered:
            keywords = self._extract_yahoo_keywords(product)
            if not keywords:
                continue

            for keyword in keywords:
                if yahoo_searches >= max_yahoo:
                    break

                # Search Yahoo with specific model
                yahoo_results = []
                for page in range(1, settings.deal_scan_max_pages + 1):
                    try:
                        page_results = await self._scraper.search(keyword, page=page)
                    except Exception as e:
                        logger.warning("Yahoo search failed for model '%s': %s", keyword, e)
                        break
                    if not page_results:
                        break
                    yahoo_results.extend(page_results)
                yahoo_searches += 1

                if not yahoo_results:
                    await asyncio.sleep(0.3)
                    continue

                # Match each Yahoo result against this Amazon product
                for yr in yahoo_results:
                    deal = await self._match_yahoo_to_amazon(yr, product)
                    if deal and (
                        deal.gross_margin_pct >= settings.deal_min_gross_margin_pct
                        and deal.gross_margin_pct <= settings.deal_max_gross_margin_pct
                        and deal.gross_profit >= settings.deal_min_gross_profit
                    ):
                        result = await self._process_deal(deal, kw, db)
                        if result:
                            new_deals.append(result)

                await asyncio.sleep(0.3)

        logger.info(
            "Generic keyword '%s': %d Yahoo searches, %d deals, %d models registered",
            kw.keyword, yahoo_searches, len(new_deals), registered,
        )
        return new_deals

    async def _find_deals(self, keyword: str):
        """Search Yahoo Auctions + targeted Keepa searches, match, score, and filter.

        Pipeline:
        1. Yahoo search (multiple pages)
        2. Classify Yahoo items: extract brand+model → targeted groups vs fallback
        3. Targeted Keepa search per (brand, model) group
        4. Fallback Keepa search for items without model numbers
        5. Match & score each Yahoo item against its Keepa candidates
        6. Filter by margin/profit thresholds
        """
        # Step 1: Yahoo search (multiple pages)
        yahoo_results = []
        max_pages = settings.deal_scan_max_pages
        for page in range(1, max_pages + 1):
            try:
                page_results = await self._scraper.search(keyword, page=page)
            except Exception as e:
                logger.warning("Yahoo search page %d failed for '%s': %s", page, keyword, e)
                break
            if not page_results:
                break
            yahoo_results.extend(page_results)

        if not yahoo_results:
            return []

        # Step 2: Classify Yahoo items by brand+model
        # targeted_groups: (brand, frozenset(models)) → [yahoo_results]
        targeted_groups: dict[tuple[str | None, frozenset[str]], list] = defaultdict(list)
        fallback_listings: list = []

        for yr in yahoo_results:
            buy_now = yr.buy_now_price if hasattr(yr, "buy_now_price") else yr.get("buy_now_price", 0)
            if buy_now <= 0:
                continue

            # Skip ended auctions
            if self._is_auction_ended(yr):
                continue

            yahoo_title = yr.title if hasattr(yr, "title") else yr.get("title", "")
            if is_apparel(yahoo_title):
                continue

            brand, models, _key_tokens = extract_product_info(yahoo_title)
            if models and buy_now >= settings.deal_min_price_for_keepa_search:
                group_key = (brand, frozenset(models))
                targeted_groups[group_key].append(yr)
            else:
                fallback_listings.append(yr)

        # Step 3: Targeted Keepa search per (brand, model) group
        max_searches = settings.deal_max_keepa_searches_per_keyword
        searches_done = 0
        targeted_keepa: dict[tuple[str | None, frozenset[str]], list[dict]] = {}

        keepa_exhausted = False
        for group_key, listings in targeted_groups.items():
            if searches_done >= max_searches or keepa_exhausted:
                # Budget exhausted — move remaining to fallback
                fallback_listings.extend(listings)
                continue

            # Proactive token check before each Keepa search
            tokens = self._keepa.tokens_left
            if tokens is not None and tokens <= 5:
                logger.info("Keepa tokens low (%s) — stopping targeted searches", tokens)
                keepa_exhausted = True
                fallback_listings.extend(listings)
                continue

            brand, models = group_key
            # Build targeted query: "brand model1 model2"
            query_parts = []
            if brand:
                query_parts.append(brand)
            query_parts.extend(sorted(models)[:2])
            query = " ".join(query_parts)

            # Clean garbage tokens that cause 0-result searches (40 tokens wasted each)
            clean_query = self._clean_keepa_query(query)
            if not clean_query:
                logger.debug("Skipping all-garbage Keepa query: '%s'", query)
                fallback_listings.extend(listings)
                continue
            if clean_query != query:
                logger.debug("Cleaned Keepa query: '%s' → '%s'", query, clean_query)
            query = clean_query

            try:
                keepa_products = await self._keepa.search_products(
                    query, stats=settings.keepa_default_stats_days
                )
                targeted_keepa[group_key] = keepa_products or []
                searches_done += 1
                if not keepa_products:
                    # 0 results — let these items try through fallback search
                    fallback_listings.extend(listings)
                logger.debug("Targeted Keepa search: '%s' → %d results", query, len(keepa_products or []))
                await asyncio.sleep(0.1)
            except KeepaApiError as e:
                logger.warning("Targeted Keepa search failed for '%s': %s", query, e)
                fallback_listings.extend(listings)
                searches_done += 1  # Count failed searches (still consumed tokens)
                # Stop all Keepa searches if throttled or tokens exhausted
                if self._keepa.is_throttled or (self._keepa.tokens_left is not None and self._keepa.tokens_left <= 0):
                    logger.info("Keepa exhausted/throttled — skipping remaining targeted searches")
                    keepa_exhausted = True
            except Exception as e:
                logger.warning("Targeted Keepa search failed for '%s': %s", query, e)
                fallback_listings.extend(listings)
                searches_done += 1  # Count failed searches

        # Step 4: Fallback Keepa search for items without model numbers
        fallback_keepa: list[dict] = []
        if fallback_listings and not keepa_exhausted:
            try:
                fallback_keepa = await self._keepa.search_products(
                    keyword, stats=settings.keepa_default_stats_days
                ) or []
            except Exception as e:
                logger.warning("Fallback Keepa search failed for '%s': %s", keyword, e)

        # Step 5: Match & score
        deals = []

        # 5a: Targeted groups
        for group_key, listings in targeted_groups.items():
            keepa_products = targeted_keepa.get(group_key)
            if not keepa_products:
                continue
            for yr in listings:
                deal = await self._match_and_score_yahoo_item(yr, keepa_products)
                if deal:
                    deals.append(deal)

        # 5b: Fallback listings
        if fallback_keepa:
            for yr in fallback_listings:
                deal = await self._match_and_score_yahoo_item(yr, fallback_keepa)
                if deal:
                    deals.append(deal)

        logger.info(
            "Keyword '%s': %d targeted groups (%d searches), %d fallback items, %d raw deals",
            keyword, len(targeted_groups), searches_done, len(fallback_listings), len(deals),
        )

        # Step 6: Filter by thresholds (min AND max margin)
        filtered = [
            d for d in deals
            if d.gross_margin_pct >= settings.deal_min_gross_margin_pct
            and d.gross_margin_pct <= settings.deal_max_gross_margin_pct
            and d.gross_profit >= settings.deal_min_gross_profit
        ]
        filtered.sort(key=lambda d: d.gross_profit, reverse=True)
        return filtered

    async def _match_and_score_yahoo_item(self, yr, keepa_products: list[dict]):
        """Match a single Yahoo item against Keepa candidates, return the best deal or None."""
        buy_now = yr.buy_now_price if hasattr(yr, "buy_now_price") else yr.get("buy_now_price", 0)
        yahoo_price = buy_now
        yahoo_title = yr.title if hasattr(yr, "title") else yr.get("title", "")

        # タイトルにジャンク等のNGワードが含まれる商品を除外
        _TITLE_EXCLUDE_WORDS = ("ジャンク",)
        if any(w in yahoo_title for w in _TITLE_EXCLUDE_WORDS):
            return None

        yr_shipping = yr.shipping_cost if hasattr(yr, "shipping_cost") else yr.get("shipping_cost")
        # None = 着払い/送料不明 → score_dealでサイズベース転送料を使用
        yahoo_shipping = yr_shipping

        best_deal = None
        best_score = -1

        for kp in keepa_products:
            amazon_title = kp.get("title") or ""
            if not amazon_title:
                continue

            result = match_products(yahoo_title, amazon_title)

            # Keepa model フィールドで型番補強
            if not result.model_match and not result.model_conflict:
                keepa_model = kp.get("model") or ""
                if keepa_model:
                    yahoo_models = extract_model_numbers_from_text(yahoo_title)
                    keepa_models = extract_model_numbers_from_text(keepa_model)
                    if yahoo_models & keepa_models:
                        result.keepa_model_match = True

            if not result.is_likely_match:
                continue

            # Require model number overlap when Yahoo item has model numbers.
            # This prevents brand-only matches (e.g., OLYMPUS OM-1 → OLYMPUS XZ-1).
            yahoo_models_extracted = extract_model_numbers_from_text(yahoo_title)
            if yahoo_models_extracted and not result.model_match and not getattr(result, "keepa_model_match", False):
                continue

            # Reject book ASINs (ISBN)
            _kp_asin = kp.get("asin", "")
            if self._is_book_asin(_kp_asin):
                continue

            # 「二度と出すな」: Yahoo title + Amazon title ペアをチェック
            try:
                from ..matcher_overrides import overrides
                if (yahoo_title, amazon_title) in overrides.never_show_pairs:
                    continue
            except ImportError:
                pass

            # Dynamic referral fee lookup via SP-API (fallback to config default)
            fee_pct = settings.deal_amazon_fee_pct
            if self._sp_api is not None:
                _asin = kp.get("asin", "")
                _stats = kp.get("stats") or {}
                _current = _stats.get("current") or []
                _used_price = _current[2] if len(_current) > 2 and _current[2] not in (None, -1) else 0
                if _asin and _used_price > 0:
                    actual_pct = await self._sp_api.get_referral_fee_pct(_asin, _used_price)
                    if actual_pct is not None:
                        fee_pct = actual_pct

            deal = score_deal(
                yahoo_price=yahoo_price,
                keepa_product=kp,
                yahoo_shipping=yahoo_shipping,
                forwarding_cost=settings.deal_forwarding_cost,
                amazon_fee_pct=fee_pct,
                good_rank_threshold=settings.keepa_good_rank_threshold,
            )
            if deal and result.score > best_score:
                # Price ratio sanity check: if Yahoo < 25% of Amazon,
                # it's likely an accessory/part, not the real product
                if deal.sell_price > 0 and yahoo_price < deal.sell_price * 0.25:
                    continue

                # 高マージン時のstrict check（型番・タイプ矛盾がないか確認）
                if deal.gross_margin_pct >= settings.deal_deep_validation_margin_threshold:
                    if not result.passes_strict_check():
                        continue

                best_score = result.score
                deal.yahoo_title = yahoo_title
                deal.yahoo_price = yahoo_price
                # yahoo_shippingがNone(着払い)の場合、score_dealが算出済みの値を維持
                if yahoo_shipping is not None:
                    deal.yahoo_shipping = yahoo_shipping
                deal.yahoo_auction_id = yr.auction_id if hasattr(yr, "auction_id") else yr.get("auction_id", "")
                deal.yahoo_url = yr.url if hasattr(yr, "url") else yr.get("url", "")
                deal.yahoo_image_url = yr.image_url if hasattr(yr, "image_url") else yr.get("image_url", "")
                best_deal = deal

        return best_deal

    async def _send_webhook(self, deal, kw: WatchedKeyword) -> None:
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
                    "footer": {"text": f"キーワード: {kw.keyword}"},
                    "thumbnail": {"url": deal.yahoo_image_url} if deal.yahoo_image_url else {},
                }],
            }
        elif self._webhook_type == "slack":
            msg = (
                f"*Deal:* {deal.yahoo_title}\n"
                f"Yahoo ¥{deal.yahoo_price:,} → Amazon中古 ¥{deal.sell_price:,}\n"
                f"粗利 ¥{deal.gross_profit:,} ({deal.gross_margin_pct}%)\n"
                f"<{yahoo_url}|Yahoo> | <{amazon_url}|Amazon>\n"
                f"キーワード: {kw.keyword}"
            )
            payload = {"text": msg}
        elif self._webhook_type == "line":
            msg = (
                f"\nDeal: {deal.yahoo_title}\n"
                f"Yahoo ¥{deal.yahoo_price:,} → Amazon中古 ¥{deal.sell_price:,}\n"
                f"粗利 ¥{deal.gross_profit:,} ({deal.gross_margin_pct}%)\n"
                f"Yahoo: {yahoo_url}\nAmazon: {amazon_url}\n"
                f"キーワード: {kw.keyword}"
            )
            payload = {"message": msg, "token": self._webhook_url}
        else:
            msg = (
                f"Deal: {deal.yahoo_title}\n"
                f"Yahoo ¥{deal.yahoo_price:,} → Amazon中古 ¥{deal.sell_price:,}\n"
                f"粗利 ¥{deal.gross_profit:,} ({deal.gross_margin_pct}%)\n"
                f"Yahoo: {yahoo_url}\nAmazon: {amazon_url}\n"
                f"キーワード: {kw.keyword}"
            )
            payload = {"message": msg}

        url = LINE_NOTIFY_URL if self._webhook_type == "line" else self._webhook_url
        success = await send_webhook(url, payload, webhook_type=self._webhook_type)
        if not success:
            logger.warning("Deal webhook failed for: %s", deal.yahoo_title[:60])
