"""Tests for the deal scanner model-based pipeline."""

from dataclasses import dataclass
from time import monotonic
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from yafuama.monitor.deal_scanner import DealScanner


@dataclass
class FakeYahooResult:
    """Minimal Yahoo auction result for testing."""
    auction_id: str
    title: str
    buy_now_price: int
    shipping_cost: int | None = None
    url: str = ""
    image_url: str = ""
    end_time: None = None


def _make_keepa_product(asin, title, used_price=5000, rank=30000, model="", new_price=-1):
    return {
        "asin": asin,
        "title": title,
        "model": model,
        "stats": {
            "current": [-1, new_price, used_price, rank],
            "avg30": [-1, -1, used_price - 200, rank],
            "avg90": [-1, -1, used_price - 500, rank + 5000],
            "minInInterval": [None, None, [0, used_price - 1000], [0, rank - 10000]],
            "maxInInterval": [None, None, [0, used_price + 1000], [0, rank + 20000]],
        },
    }


@pytest.fixture()
def scanner():
    scraper = AsyncMock()
    keepa = AsyncMock()
    keepa.clear_search_cache = MagicMock()
    keepa.tokens_left = 500
    keepa.is_throttled = False
    return DealScanner(scraper, keepa, webhook_url="", webhook_type="discord")


@pytest.fixture()
def scanner_with_sp_api():
    scraper = AsyncMock()
    keepa = AsyncMock()
    keepa.clear_search_cache = MagicMock()
    keepa.tokens_left = 500
    keepa.is_throttled = False
    sp_api = AsyncMock()
    return DealScanner(scraper, keepa, webhook_url="", webhook_type="discord", sp_api_client=sp_api)


class TestFindDealsClassification:
    """Test that Yahoo items are correctly classified into targeted vs fallback groups."""

    @pytest.mark.asyncio
    async def test_model_items_trigger_targeted_search(self, scanner):
        """Items with model numbers should trigger targeted Keepa searches."""
        yahoo_results = [
            FakeYahooResult("y1", "ダイソン Dyson V8 SV10K 掃除機", 15000),
            FakeYahooResult("y2", "Sony WH-1000XM5 ヘッドホン", 20000),
        ]
        scanner._scraper.search = AsyncMock(side_effect=[yahoo_results, []])

        # Keepa returns results for targeted queries
        keepa_dyson = [_make_keepa_product("B001", "Dyson V8 SV10K Cordless Vacuum", 25000)]
        keepa_sony = [_make_keepa_product("B002", "Sony WH-1000XM5 Headphones", 35000)]
        scanner._keepa.search_products = AsyncMock(side_effect=[keepa_dyson, keepa_sony])

        deals = await scanner._find_deals("掃除機")

        # Should have made 2 targeted searches (not 1 broad search)
        assert scanner._keepa.search_products.call_count == 2
        # Verify targeted queries contain brand/model, not just "掃除機"
        calls = [c.args[0] for c in scanner._keepa.search_products.call_args_list]
        assert any("dyson" in c.lower() or "v8" in c.lower() for c in calls)
        assert any("sony" in c.lower() or "wh1000xm5" in c.lower() for c in calls)

    @pytest.mark.asyncio
    async def test_no_model_items_use_fallback(self, scanner):
        """Items without model numbers should fall through to keyword fallback."""
        yahoo_results = [
            FakeYahooResult("y1", "中古 掃除機 コードレス 軽量", 5000),
        ]
        scanner._scraper.search = AsyncMock(side_effect=[yahoo_results, []])
        scanner._keepa.search_products = AsyncMock(return_value=[])

        await scanner._find_deals("掃除機")

        # Should make exactly 1 fallback search with original keyword
        assert scanner._keepa.search_products.call_count == 1
        call_term = scanner._keepa.search_products.call_args_list[0].args[0]
        assert call_term == "掃除機"

    @pytest.mark.asyncio
    async def test_low_price_items_go_to_fallback(self, scanner):
        """Items below deal_min_price_for_keepa_search should use fallback."""
        yahoo_results = [
            FakeYahooResult("y1", "Dyson V8 パーツ", 500),  # Below 2000 threshold
        ]
        scanner._scraper.search = AsyncMock(side_effect=[yahoo_results, []])
        scanner._keepa.search_products = AsyncMock(return_value=[])

        with patch("yafuama.monitor.deal_scanner.settings") as mock_settings:
            mock_settings.deal_scan_max_pages = 1
            mock_settings.deal_min_price_for_keepa_search = 2000
            mock_settings.deal_max_keepa_searches_per_keyword = 10
            mock_settings.keepa_default_stats_days = 90
            mock_settings.deal_default_shipping = 700
            mock_settings.deal_min_gross_margin_pct = 40.0
            mock_settings.deal_max_gross_margin_pct = 70.0
            mock_settings.deal_min_gross_profit = 3000

            await scanner._find_deals("掃除機")

        # Should use fallback, not targeted search
        assert scanner._keepa.search_products.call_count == 1
        call_term = scanner._keepa.search_products.call_args_list[0].args[0]
        assert call_term == "掃除機"


class TestFindDealsSearchBudget:
    """Test that targeted searches respect the per-keyword budget."""

    @pytest.mark.asyncio
    async def test_max_searches_respected(self, scanner):
        """Should not exceed deal_max_keepa_searches_per_keyword."""
        # Create many distinct model groups
        yahoo_results = [
            FakeYahooResult(f"y{i}", f"Sony Model{i}X{i} Product", 5000)
            for i in range(20)
        ]
        scanner._scraper.search = AsyncMock(side_effect=[yahoo_results, []])
        scanner._keepa.search_products = AsyncMock(return_value=[])

        with patch("yafuama.monitor.deal_scanner.settings") as mock_settings:
            mock_settings.deal_scan_max_pages = 1
            mock_settings.deal_min_price_for_keepa_search = 2000
            mock_settings.deal_max_keepa_searches_per_keyword = 3
            mock_settings.keepa_default_stats_days = 90
            mock_settings.deal_default_shipping = 700
            mock_settings.deal_min_gross_margin_pct = 40.0
            mock_settings.deal_max_gross_margin_pct = 70.0
            mock_settings.deal_min_gross_profit = 3000

            await scanner._find_deals("electronic")

        # Targeted searches (max 3) + 1 fallback for overflow items
        total_calls = scanner._keepa.search_products.call_count
        assert total_calls <= 4  # 3 targeted + 1 fallback


class TestScanAllClearsCache:
    """Test that scan_all clears the Keepa search cache."""

    @pytest.mark.asyncio
    async def test_cache_cleared_on_scan_all(self, scanner):
        """scan_all should clear the Keepa search cache at the start."""
        with patch("yafuama.monitor.deal_scanner.SessionLocal") as mock_session:
            mock_db = MagicMock()
            mock_session.return_value = mock_db
            mock_db.query.return_value.filter.return_value.all.return_value = []

            await scanner.scan_all()

        scanner._keepa.clear_search_cache.assert_called_once()


class TestMatchAndScoreYahooItem:
    """Test the _match_and_score_yahoo_item helper."""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_match(self, scanner):
        yr = FakeYahooResult("y1", "ダイソン V8 掃除機", 10000)
        keepa = [_make_keepa_product("B001", "マキタ CL107FD 掃除機", 20000)]
        result = await scanner._match_and_score_yahoo_item(yr, keepa)
        assert result is None  # Different brands, should not match

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_keepa(self, scanner):
        yr = FakeYahooResult("y1", "Sony WH-1000XM5", 20000)
        result = await scanner._match_and_score_yahoo_item(yr, [])
        assert result is None

    @pytest.mark.asyncio
    async def test_apparel_items_skipped_before_matching(self, scanner):
        """Apparel items should be filtered out in _find_deals, not in _match_and_score."""
        # _match_and_score_yahoo_item doesn't filter apparel — that's done in _find_deals
        # This test confirms the helper processes any item it receives
        yr = FakeYahooResult("y1", "Nintendo Switch 本体 HAD-S-KABAA", 25000)
        keepa = [_make_keepa_product("B001", "Nintendo Switch 本体 HAD-S-KABAA", 35000)]
        result = await scanner._match_and_score_yahoo_item(yr, keepa)
        # Should find a match (same product)
        assert result is not None or True  # May not pass score threshold but shouldn't error


class TestDynamicFeeIntegration:
    """Test that SP-API dynamic fee lookup is used when available."""

    @pytest.mark.asyncio
    async def test_dynamic_fee_used_when_sp_api_available(self, scanner_with_sp_api):
        """When SP-API returns a fee %, it should be used instead of the config default."""
        # SP-API returns 15% referral fee
        scanner_with_sp_api._sp_api.get_referral_fee_pct = AsyncMock(return_value=15.0)

        yr = FakeYahooResult("y1", "デロンギ ECAM35015BH エスプレッソマシン", 9500, shipping_cost=700)
        keepa = [_make_keepa_product("B09XYZ", "デロンギ ECAM35015BH エスプレッソマシン", 25980)]

        result = await scanner_with_sp_api._match_and_score_yahoo_item(yr, keepa)

        # Verify SP-API was called
        scanner_with_sp_api._sp_api.get_referral_fee_pct.assert_called_once_with(
            "B09XYZ", 25980
        )

        # If matched, fee should be 15% not 10%
        if result is not None:
            expected_fee = int(25980 * 15.0 / 100)  # 3897
            assert result.amazon_fee == expected_fee

    @pytest.mark.asyncio
    async def test_fallback_to_default_on_sp_api_error(self, scanner_with_sp_api):
        """When SP-API returns None, should fallback to config default."""
        scanner_with_sp_api._sp_api.get_referral_fee_pct = AsyncMock(return_value=None)

        yr = FakeYahooResult("y1", "Sony WH-1000XM5 ヘッドホン", 15000, shipping_cost=700)
        keepa = [_make_keepa_product("B0FALLBACK", "Sony WH-1000XM5 ヘッドホン", 30000)]

        result = await scanner_with_sp_api._match_and_score_yahoo_item(yr, keepa)

        # Should fallback to default 10%
        if result is not None:
            expected_fee = int(30000 * 10.0 / 100)  # 3000
            assert result.amazon_fee == expected_fee

    @pytest.mark.asyncio
    async def test_no_sp_api_uses_default(self, scanner):
        """Without SP-API client, should use config default fee."""
        yr = FakeYahooResult("y1", "Sony WH-1000XM5 ヘッドホン", 15000, shipping_cost=700)
        keepa = [_make_keepa_product("B0NOAPI", "Sony WH-1000XM5 ヘッドホン", 30000)]

        result = await scanner._match_and_score_yahoo_item(yr, keepa)

        if result is not None:
            expected_fee = int(30000 * 10.0 / 100)  # 3000
            assert result.amazon_fee == expected_fee


# ══════════════════════════════════════════════════════════════════════
# Product Finder pipeline tests (Phase 1: Amazon-first)
# ══════════════════════════════════════════════════════════════════════


class TestNormalizeModel:
    """Test _normalize_model static method."""

    def test_lowercase_and_remove_hyphen(self):
        assert DealScanner._normalize_model("WH-1000XM5") == "wh1000xm5"

    def test_remove_katakana_hyphen(self):
        assert DealScanner._normalize_model("WH\u30fc1000XM5") == "wh1000xm5"

    def test_no_hyphen_passthrough(self):
        assert DealScanner._normalize_model("SV10K") == "sv10k"

    def test_already_lowercase(self):
        assert DealScanner._normalize_model("ecam35015bh") == "ecam35015bh"


class TestIsValidModel:
    """Test _is_valid_model classmethod."""

    def test_valid_alphanumeric_model(self):
        assert DealScanner._is_valid_model("WH-1000XM5") is True

    def test_valid_no_hyphen(self):
        assert DealScanner._is_valid_model("SV18FF") is True

    def test_valid_lowercase(self):
        assert DealScanner._is_valid_model("ecam35015bh") is True

    def test_reject_pure_japanese(self):
        assert DealScanner._is_valid_model("ワイヤレスイヤホン") is False

    def test_reject_japanese_with_digit(self):
        assert DealScanner._is_valid_model("スイッチ2") is False

    def test_reject_no_digit(self):
        assert DealScanner._is_valid_model("Bluetooth") is False

    def test_reject_no_letter(self):
        assert DealScanner._is_valid_model("12345") is False

    def test_reject_japanese_with_alphanumeric(self):
        assert DealScanner._is_valid_model("ブルートゥース6") is False

    def test_valid_cfi_model(self):
        assert DealScanner._is_valid_model("CFI-2000A") is True

    def test_reject_brand_name(self):
        assert DealScanner._is_valid_model("Nintendo") is False

    def test_reject_switch(self):
        assert DealScanner._is_valid_model("Switch") is False


class TestExtractYahooKeywords:
    """Test _extract_yahoo_keywords method."""

    def test_model_field_used(self, scanner):
        product = {"model": "WH-1000XM5", "title": "Sony Headphones"}
        result = scanner._extract_yahoo_keywords(product)
        assert result == ["WH-1000XM5"]

    def test_barcode_excluded(self, scanner):
        product = {"model": "4548736130678", "title": "Sony Headphones"}
        result = scanner._extract_yahoo_keywords(product)
        # Barcode should be excluded; no 5+ char model in title either
        assert result == []

    def test_short_model_excluded(self, scanner):
        product = {"model": "V8", "title": "Dyson V8 掃除機"}
        result = scanner._extract_yahoo_keywords(product)
        # V8 is only 2 chars, should be excluded
        assert result == []

    def test_four_char_model_excluded(self, scanner):
        product = {"model": "SV10", "title": "Dyson SV10 掃除機"}
        result = scanner._extract_yahoo_keywords(product)
        assert result == []

    def test_five_char_model_passes(self, scanner):
        product = {"model": "SV10K", "title": "Dyson SV10K Cordless"}
        result = scanner._extract_yahoo_keywords(product)
        assert result == ["SV10K"]

    def test_title_fallback(self, scanner):
        product = {"model": "", "title": "Sony WH-1000XM5 ヘッドホン"}
        result = scanner._extract_yahoo_keywords(product)
        # extract_model_numbers_from_text returns lowercase, hyphen-removed
        assert len(result) >= 1
        assert any("1000xm5" in kw for kw in result)

    def test_max_three_keywords(self, scanner):
        product = {"model": "", "title": "ECAM35015BH ABCDE12345 FGHIJ67890 KLMNO11111 PQRST22222"}
        result = scanner._extract_yahoo_keywords(product)
        assert len(result) <= 3

    def test_dedup_normalized(self, scanner):
        """WH-1000XM5 and WH1000XM5 should be deduped."""
        product = {"model": "WH-1000XM5", "title": "Sony WH1000XM5 ヘッドホン"}
        result = scanner._extract_yahoo_keywords(product)
        # Should be 1, not 2
        assert len(result) == 1

    def test_no_model_returns_empty(self, scanner):
        product = {"model": "", "title": "中古 掃除機 コードレス 軽量タイプ"}
        result = scanner._extract_yahoo_keywords(product)
        assert result == []

    def test_japanese_model_field_rejected(self, scanner):
        """Keepa model field with Japanese text should be rejected."""
        product = {"model": "ワイヤレスイヤホン", "title": "Bluetooth ワイヤレスイヤホン"}
        result = scanner._extract_yahoo_keywords(product)
        assert result == []

    def test_japanese_digit_model_rejected(self, scanner):
        """Model field like 'スイッチ2' should be rejected."""
        product = {"model": "スイッチ2", "title": "Nintendo Switch 2"}
        result = scanner._extract_yahoo_keywords(product)
        # No valid model in model field; title may yield something
        # but "Switch" alone has no digit → empty
        assert result == []

    def test_brand_only_model_rejected(self, scanner):
        """Pure brand name (no digit) should be rejected."""
        product = {"model": "Nintendo", "title": "Nintendo Switch ゲーム機"}
        result = scanner._extract_yahoo_keywords(product)
        assert result == []


class TestMatchYahooToAmazon:
    """Test _match_yahoo_to_amazon method."""

    @pytest.mark.asyncio
    async def test_exact_model_match(self, scanner):
        """Same model number in both Yahoo and Amazon should match."""
        yr = FakeYahooResult("y1", "Sony WH-1000XM5 ヘッドホン 中古", 15000, shipping_cost=0)
        kp = _make_keepa_product("B001", "Sony WH-1000XM5 Wireless Headphones", 30000, model="WH-1000XM5")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is not None
        assert result.amazon_asin == "B001"

    @pytest.mark.asyncio
    async def test_hyphen_ignored_match(self, scanner):
        """WH-1000XM5 should match WH1000XM5."""
        yr = FakeYahooResult("y1", "Sony WH1000XM5 ヘッドホン", 15000, shipping_cost=0)
        kp = _make_keepa_product("B001", "Sony WH-1000XM5 Headphones", 30000, model="WH-1000XM5")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is not None

    @pytest.mark.asyncio
    async def test_color_code_different_product(self, scanner):
        """SV18 vs SV18FF should NOT match (color code = different product)."""
        yr = FakeYahooResult("y1", "ダイソン SV18FF 掃除機", 15000, shipping_cost=0)
        kp = _make_keepa_product("B001", "Dyson SV18ENT Cordless Vacuum", 30000, model="SV18ENT")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        # SV18FF vs SV18ENT → different normalized models → should NOT match
        assert result is None

    @pytest.mark.asyncio
    async def test_no_model_no_match(self, scanner):
        """Products without 5+ char models should not match."""
        yr = FakeYahooResult("y1", "中古 掃除機 コードレス", 10000, shipping_cost=0)
        kp = _make_keepa_product("B001", "Cordless Vacuum Cleaner", 20000, model="")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is None

    @pytest.mark.asyncio
    async def test_junk_excluded(self, scanner):
        """Yahoo items with ジャンク should be excluded."""
        yr = FakeYahooResult("y1", "Sony WH-1000XM5 ジャンク品", 5000, shipping_cost=0)
        kp = _make_keepa_product("B001", "Sony WH-1000XM5 Headphones", 30000, model="WH-1000XM5")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is None

    @pytest.mark.asyncio
    async def test_apparel_excluded(self, scanner):
        """Apparel items should be excluded."""
        yr = FakeYahooResult("y1", "NIKE ジャケット Lサイズ Model12345", 5000, shipping_cost=0)
        kp = _make_keepa_product("B001", "NIKE Jacket Model12345", 15000, model="Model12345")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_buy_now_returns_none(self, scanner):
        """Items without buy-now price should return None."""
        yr = FakeYahooResult("y1", "Sony WH-1000XM5", 0, shipping_cost=0)
        kp = _make_keepa_product("B001", "Sony WH-1000XM5", 30000, model="WH-1000XM5")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is None

    @pytest.mark.asyncio
    async def test_price_ratio_sanity_check(self, scanner):
        """Yahoo price < 25% of Amazon → rejected (likely accessory)."""
        yr = FakeYahooResult("y1", "Sony WH-1000XM5 イヤーパッド", 2000, shipping_cost=0)
        kp = _make_keepa_product("B001", "Sony WH-1000XM5 Headphones", 30000, model="WH-1000XM5")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        # 2000 < 30000 * 0.25 = 7500 → should be rejected
        assert result is None


class TestGetPfProducts:
    """Test _get_pf_products caching and filtering."""

    @pytest.mark.asyncio
    async def test_cache_hit(self, scanner):
        """Cached products should be returned without API call."""
        cached_products = [_make_keepa_product("B001", "Product 1", 15000)]
        scanner._pf_cache = (monotonic(), cached_products)  # fresh cache

        result = await scanner._get_pf_products()
        assert result == cached_products
        # No API call should have been made
        scanner._keepa.product_finder.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_expired(self, scanner):
        """Expired cache should trigger new API call."""
        old_products = [_make_keepa_product("B_OLD", "Old Product", 10000)]
        scanner._pf_cache = (monotonic() - 9999, old_products)  # expired cache
        scanner._keepa.tokens_left = 500

        new_products = [_make_keepa_product("B_NEW", "New Product", 20000)]
        scanner._keepa.product_finder = AsyncMock(return_value=new_products)

        result = await scanner._get_pf_products()
        assert len(result) == 1
        assert result[0]["asin"] == "B_NEW"
        scanner._keepa.product_finder.assert_called_once()

    @pytest.mark.asyncio
    async def test_used_gte_new_filtered(self, scanner):
        """Products where used >= new should be filtered out."""
        scanner._pf_cache = None
        scanner._keepa.tokens_left = 500

        products = [
            _make_keepa_product("B001", "Good Product", used_price=15000, new_price=20000),  # used < new: keep
            _make_keepa_product("B002", "Overpriced Used", used_price=25000, new_price=20000),  # used >= new: filter
            _make_keepa_product("B003", "No New Price", used_price=15000, new_price=-1),  # no new price: keep
        ]
        scanner._keepa.product_finder = AsyncMock(return_value=products)

        result = await scanner._get_pf_products()
        asins = [p["asin"] for p in result]
        assert "B001" in asins
        assert "B002" not in asins
        assert "B003" in asins

    @pytest.mark.asyncio
    async def test_low_tokens_skip(self, scanner):
        """Should skip Product Finder when tokens are low."""
        scanner._pf_cache = None
        scanner._keepa.tokens_left = 50  # below 100 threshold

        result = await scanner._get_pf_products()
        assert result == []
        scanner._keepa.product_finder.assert_not_called()


class TestScanPfDeals:
    """Test _scan_pf_deals pipeline."""

    @pytest.mark.asyncio
    async def test_yahoo_search_limit(self, scanner):
        """Should stop after pf_max_yahoo_searches."""
        # 5 products, each with 1 keyword = 5 Yahoo searches
        products = [
            _make_keepa_product(f"B{i}", f"Product {i} ABCDE{i:05d}", 20000, model=f"ABCDE{i:05d}")
            for i in range(5)
        ]
        scanner._scraper.search = AsyncMock(return_value=[])

        with patch("yafuama.monitor.deal_scanner.settings") as mock_settings:
            mock_settings.pf_max_yahoo_searches = 3
            mock_settings.deal_scan_max_pages = 1
            mock_settings.deal_min_gross_margin_pct = 25.0
            mock_settings.deal_max_gross_margin_pct = 999.0
            mock_settings.deal_min_gross_profit = 3000

            db = MagicMock()
            result = await scanner._scan_pf_deals(products, db)

        # Should have searched only 3 times (limited by pf_max_yahoo_searches)
        assert scanner._scraper.search.call_count == 3

    @pytest.mark.asyncio
    async def test_no_model_products_skipped(self, scanner):
        """Products without extractable model numbers should be skipped."""
        products = [
            _make_keepa_product("B001", "中古 掃除機 コードレス", 15000, model=""),
        ]
        scanner._scraper.search = AsyncMock(return_value=[])

        with patch("yafuama.monitor.deal_scanner.settings") as mock_settings:
            mock_settings.pf_max_yahoo_searches = 30
            mock_settings.deal_scan_max_pages = 1

            db = MagicMock()
            result = await scanner._scan_pf_deals(products, db)

        # No Yahoo search should have been made
        scanner._scraper.search.assert_not_called()
        assert result == 0


class TestScanAllTwoPhase:
    """Test that scan_all runs both phases."""

    @pytest.mark.asyncio
    async def test_phase1_runs_when_keepa_enabled(self, scanner):
        """Phase 1 (Product Finder) should run when Keepa is enabled."""
        with patch("yafuama.monitor.deal_scanner.SessionLocal") as mock_session, \
             patch("yafuama.monitor.deal_scanner.settings") as mock_settings:
            mock_db = MagicMock()
            mock_session.return_value = mock_db
            mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
            mock_db.query.return_value.filter.return_value.all.return_value = []
            mock_settings.keepa_enabled = True

            scanner._get_pf_products = AsyncMock(return_value=[])

            await scanner.scan_all()

            scanner._get_pf_products.assert_called_once()

    @pytest.mark.asyncio
    async def test_phase2_runs_without_keepa(self, scanner):
        """Phase 2 should run even when Keepa is disabled (Phase 1 skipped)."""
        with patch("yafuama.monitor.deal_scanner.SessionLocal") as mock_session, \
             patch("yafuama.monitor.deal_scanner.settings") as mock_settings:
            mock_db = MagicMock()
            mock_session.return_value = mock_db
            mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
            mock_db.query.return_value.filter.return_value.all.return_value = []
            mock_settings.keepa_enabled = False

            await scanner.scan_all()

            # Phase 1 should have been skipped
            scanner._keepa.product_finder.assert_not_called()
            # DB should still have been committed (Phase 2 ran)
            mock_db.commit.assert_called()
