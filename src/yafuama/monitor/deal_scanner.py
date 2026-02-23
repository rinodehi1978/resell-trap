"""Automated deal scanner: periodically searches watched keywords for profitable deals."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..database import SessionLocal
from ..keepa.analyzer import score_deal
from ..matcher import (
    extract_model_numbers_from_text,
    extract_product_info,
    is_apparel,
    match_products,
)
from ..models import DealAlert, WatchedKeyword
from ..notifier.webhook import LINE_NOTIFY_URL, send_webhook

logger = logging.getLogger(__name__)

YAHOO_AUCTION_URL = "https://auctions.yahoo.co.jp/jp/auction/{}"


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
        self._deep_validation_count = 0
        self._sp_api = sp_api_client

    async def scan_all(self) -> None:
        """Scan all active watched keywords and notify on new deals.

        Keywords are scanned in rotation order: least-recently-scanned first
        (never-scanned keywords get top priority). Scanning stops early when
        Keepa tokens are exhausted, so the next cycle picks up where this one
        left off.
        """
        self._deep_validation_count = 0
        self._keepa.clear_search_cache()
        db = SessionLocal()
        try:
            keywords = (
                db.query(WatchedKeyword)
                .filter(WatchedKeyword.is_active == True)  # noqa: E712
                .order_by(
                    # NULL (never scanned) first, then oldest scan first
                    WatchedKeyword.last_scanned_at.is_(None).desc(),
                    WatchedKeyword.last_scanned_at.asc(),
                )
                .all()
            )
            if not keywords:
                return

            scanned = 0
            for kw in keywords:
                # Check Keepa token budget before each keyword
                tokens = self._keepa.tokens_left
                if tokens is not None and tokens <= 5:
                    logger.info(
                        "Keepa tokens low (%s remaining), pausing scan after %d/%d keywords. "
                        "Remaining keywords will be prioritized next cycle.",
                        tokens, scanned, len(keywords),
                    )
                    break

                try:
                    new_deals = await self._scan_keyword(kw, db)
                    kw.last_scanned_at = datetime.now(timezone.utc)
                    kw.total_scans += 1
                    if new_deals:
                        kw.scans_since_last_deal = 0
                    else:
                        kw.scans_since_last_deal += 1
                    scanned += 1
                except Exception as e:
                    logger.warning("Error scanning keyword '%s': %s", kw.keyword, e)

            # Auto-cleanup underperforming keywords
            self._cleanup_keywords(db)

            logger.info("Scan cycle complete: %d/%d keywords scanned", scanned, len(keywords))
            db.commit()
        except Exception as e:
            logger.exception("Error in deal scan loop: %s", e)
            db.rollback()
        finally:
            db.close()

    def _cleanup_keywords(self, db) -> None:
        """Auto-cleanup underperforming keywords after each scan cycle.

        Rules:
        - AI-generated + 0 deals + 10+ scans → DELETE
        - Manual + 0 deals + 50+ scans → DELETE
        - Manual + has deals + 50+ scans since last deal → PAUSE (is_active=False)
        """
        cleanup_threshold_manual = 50
        cleanup_threshold_ai = 10

        keywords = db.query(WatchedKeyword).filter(WatchedKeyword.is_active == True).all()  # noqa: E712
        for kw in keywords:
            # AI-generated: delete after 10 scans with no results
            if kw.source != "manual" and kw.total_deals_found == 0 and kw.total_scans >= cleanup_threshold_ai:
                logger.info(
                    "Auto-deleting AI keyword '%s': %d scans, 0 deals",
                    kw.keyword, kw.total_scans,
                )
                db.delete(kw)
                continue

            # Manual + never found a deal: delete after 50 scans
            if kw.source == "manual" and kw.total_deals_found == 0 and kw.total_scans >= cleanup_threshold_manual:
                logger.info(
                    "Auto-deleting manual keyword '%s': %d scans, 0 deals",
                    kw.keyword, kw.total_scans,
                )
                db.delete(kw)
                continue

            # Manual + has deals but dormant: pause after 50 consecutive scans without new deal
            if kw.source == "manual" and kw.total_deals_found > 0 and kw.scans_since_last_deal >= cleanup_threshold_manual:
                logger.info(
                    "Auto-pausing manual keyword '%s': %d scans since last deal",
                    kw.keyword, kw.scans_since_last_deal,
                )
                kw.is_active = False

    async def scan_keyword_by_id(self, keyword_id: int) -> list[dict]:
        """Manually trigger scan for a single keyword. Returns list of new deal dicts."""
        db = SessionLocal()
        try:
            kw = db.query(WatchedKeyword).filter(WatchedKeyword.id == keyword_id).first()
            if not kw:
                return []
            new_deals = await self._scan_keyword(kw, db)
            kw.last_scanned_at = datetime.now(timezone.utc)
            db.commit()
            return new_deals
        except Exception as e:
            logger.exception("Error in manual scan for keyword %d: %s", keyword_id, e)
            db.rollback()
            return []
        finally:
            db.close()

    async def _scan_keyword(self, kw: WatchedKeyword, db) -> list[dict]:
        """Search Yahoo + Keepa, score deals, notify on new ones."""
        deals = await self._find_deals(kw.keyword)
        if not deals:
            return []

        new_deals = []
        for deal in deals:
            # Check if already notified
            existing = (
                db.query(DealAlert)
                .filter(
                    DealAlert.yahoo_auction_id == deal.yahoo_auction_id,
                    DealAlert.amazon_asin == deal.amazon_asin,
                )
                .first()
            )
            if existing:
                continue

            # Update keyword stats for learning loop
            kw.total_deals_found += 1
            kw.total_gross_profit += deal.gross_profit

            # Send notification
            await self._send_webhook(deal, kw)

            # Record alert (use savepoint to handle duplicate gracefully)
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
                continue

            # 型番シリーズ横展開: 利益Deal発見時に兄弟モデル候補を自動生成
            if deal.gross_profit >= settings.series_expansion_min_profit:
                self._enqueue_series_candidates(deal, kw, db)

            new_deals.append({
                "yahoo_title": deal.yahoo_title,
                "yahoo_price": deal.yahoo_price,
                "sell_price": deal.sell_price,
                "gross_profit": deal.gross_profit,
                "gross_margin_pct": deal.gross_margin_pct,
            })

        logger.info(
            "Keyword '%s': found %d deals, %d new",
            kw.keyword, len(deals), len(new_deals),
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

        for group_key, listings in targeted_groups.items():
            if searches_done >= max_searches:
                # Budget exhausted — move remaining to fallback
                fallback_listings.extend(listings)
                continue

            brand, models = group_key
            # Build targeted query: "brand model1 model2"
            query_parts = []
            if brand:
                query_parts.append(brand)
            query_parts.extend(sorted(models)[:2])
            query = " ".join(query_parts)

            try:
                keepa_products = await self._keepa.search_products(
                    query, stats=settings.keepa_default_stats_days
                )
                targeted_keepa[group_key] = keepa_products or []
                searches_done += 1
                logger.debug("Targeted Keepa search: '%s' → %d results", query, len(keepa_products or []))
            except Exception as e:
                logger.warning("Targeted Keepa search failed for '%s': %s", query, e)
                fallback_listings.extend(listings)

        # Step 4: Fallback Keepa search for items without model numbers
        fallback_keepa: list[dict] = []
        if fallback_listings:
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
            if not result.model_match:
                keepa_model = kp.get("model") or ""
                if keepa_model:
                    yahoo_models = extract_model_numbers_from_text(yahoo_title)
                    keepa_models = extract_model_numbers_from_text(keepa_model)
                    if yahoo_models & keepa_models:
                        result.keepa_model_match = True

            if not result.is_likely_match:
                continue

            # Check rejection-learned blocked pairs and never-show pairs
            try:
                from ..matcher_overrides import overrides
                _yr_id = yr.auction_id if hasattr(yr, "auction_id") else yr.get("auction_id", "")
                _kp_asin = kp.get("asin", "")
                if (_yr_id, _kp_asin) in overrides.blocked_pairs:
                    continue
                # 「二度と出すな」: Yahoo title + Amazon title ペアをチェック
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

    def _enqueue_series_candidates(self, deal, kw, db) -> None:
        """利益Deal発見時に兄弟モデルのKeywordCandidateを即座にDB登録。"""
        from ..ai.generator import _decompose_model, _guess_step
        from ..models import KeywordCandidate

        brand, models, _ = extract_product_info(deal.yahoo_title)
        if not models:
            return

        # 既存キーワード・候補を取得して重複排除
        existing_kws = {
            row[0].lower()
            for row in db.query(WatchedKeyword.keyword).all()
        }
        existing_candidates = {
            row[0].lower()
            for row in db.query(KeywordCandidate.keyword)
            .filter(KeywordCandidate.status.notin_(["rejected"]))
            .all()
        }
        existing = existing_kws | existing_candidates

        count = 0
        for model in models:
            parts = _decompose_model(model)
            if not parts:
                continue
            prefix, num, suffix = parts
            step = _guess_step(num)

            for offset in [-2, -1, 1, 2]:
                sibling_num = num + offset * step
                if sibling_num <= 0:
                    continue
                sibling_model = f"{prefix}{sibling_num}{suffix}"
                keyword = f"{brand} {sibling_model}" if brand else sibling_model

                if keyword.lower() in existing:
                    continue

                db.add(KeywordCandidate(
                    keyword=keyword,
                    strategy="series",
                    confidence=0.75,
                    parent_keyword_id=kw.id,
                    reasoning=f"利益確認済み「{brand or ''} {model}」(¥{deal.gross_profit:,})のシリーズ展開",
                    status="pending",
                ))
                existing.add(keyword.lower())
                count += 1

                if count >= settings.series_expansion_max_siblings:
                    if count > 0:
                        logger.info("Series expansion: %d candidates from '%s'", count, deal.yahoo_title[:50])
                    return

        if count > 0:
            logger.info("Series expansion: %d candidates from '%s'", count, deal.yahoo_title[:50])

    async def _deep_validate_deal(self, yahoo_auction_id: str, yahoo_title: str) -> bool:
        """ヤフオク説明文を取得し、アクセサリー/付属品でないか検証。

        Returns True = pass（本体の可能性が高い）、False = reject（アクセサリーの疑い）
        """
        self._deep_validation_count += 1
        try:
            description = await self._scraper.fetch_auction_description(yahoo_auction_id)
        except Exception as e:
            logger.warning("Deep validation fetch failed for %s: %s", yahoo_auction_id, e)
            return True  # 取得失敗時は通過（保守的判断）

        if not description:
            return True  # 説明文なしは通過

        combined_text = yahoo_title + " " + description
        if extract_accessory_signals_from_text(combined_text):
            logger.info(
                "Deep validation rejected %s: accessory signal in description",
                yahoo_auction_id,
            )
            return False

        return True

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
                                " (送料無料)" if deal.yahoo_shipping == 0 else f" (+送料¥{deal.yahoo_shipping:,})"
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
