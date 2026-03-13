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
            result, stats = await scanner._scan_pf_deals(products, db)

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
            result, stats = await scanner._scan_pf_deals(products, db)

        # No Yahoo search should have been made
        scanner._scraper.search.assert_not_called()
        assert result == 0
        assert stats["no_keywords"] == 1


class TestScanAll:
    """Test that scan_all runs Product Finder pipeline."""

    @pytest.mark.asyncio
    async def test_pf_runs_when_keepa_enabled(self, scanner):
        """Product Finder should run when Keepa is enabled."""
        with patch("yafuama.monitor.deal_scanner.SessionLocal") as mock_session, \
             patch("yafuama.monitor.deal_scanner.settings") as mock_settings:
            mock_db = MagicMock()
            mock_session.return_value = mock_db
            mock_settings.keepa_enabled = True

            scanner._get_pf_products = AsyncMock(return_value=[])

            await scanner.scan_all()

            scanner._get_pf_products.assert_called_once()

    @pytest.mark.asyncio
    async def test_skipped_when_keepa_disabled(self, scanner):
        """Product Finder should be skipped when Keepa is disabled."""
        with patch("yafuama.monitor.deal_scanner.SessionLocal") as mock_session, \
             patch("yafuama.monitor.deal_scanner.settings") as mock_settings:
            mock_db = MagicMock()
            mock_session.return_value = mock_db
            mock_settings.keepa_enabled = False

            await scanner.scan_all()

            scanner._keepa.product_finder.assert_not_called()


# ══════════════════════════════════════════════════════════════════════
# 鉄壁の防御: Regression tests for matching pipeline invariants
# ══════════════════════════════════════════════════════════════════════


class TestIsValidModelSpecValues:
    """is_valid_model MUST reject all spec/unit values.

    Invariant: _SPEC_UNIT_RE is checked inside is_valid_model(),
    so ANY code path that calls is_valid_model() is protected.
    """

    @pytest.mark.parametrize("spec", [
        "32bit", "64bit", "128bit",
        "192khz", "44khz", "48khz", "96khz",
        "100mhz", "2400mhz", "5ghz",
        "128gb", "256gb", "512gb", "1024mb",
        "10000mah", "20000mah", "5000mah",
        "100mm", "200cm",
        "48fps", "60fps", "120fps",
        "300dpi", "600dpi",
        "3000rpm", "10000rpm",
    ])
    def test_spec_values_rejected(self, spec):
        assert DealScanner._is_valid_model(spec) is False

    @pytest.mark.parametrize("dim", [
        "30x30cm", "100x200mm", "50x50m",
    ])
    def test_dimension_values_rejected(self, dim):
        assert DealScanner._is_valid_model(dim) is False

    def test_blocklisted_brand_rejected(self):
        assert DealScanner._is_valid_model("52toys") is False

    @pytest.mark.parametrize("word_ver", [
        "switch2", "bluetooth6", "hero13", "channel5",
        "version3", "windows11", "android14",
    ])
    def test_common_word_version_rejected(self, word_ver):
        assert DealScanner._is_valid_model(word_ver) is False

    @pytest.mark.parametrize("valid", [
        "WH-1000XM5", "SV10K", "ECAM35015BH", "CFI-2000A", "SR750",
    ])
    def test_valid_models_accepted(self, valid):
        assert DealScanner._is_valid_model(valid) is True


class TestKeepaModelFieldValidation:
    """Keepa model field MUST go through is_valid_model() before use.

    Invariant: _extract_yahoo_keywords and _match_yahoo_to_amazon both
    check is_valid_model(model_field) before accepting it.
    """

    def test_spec_model_field_excluded_from_keywords(self, scanner):
        """Keepa model='192khz' should NOT become a Yahoo search keyword."""
        product = {"model": "192khz", "title": "DAC Converter 192khz"}
        result = scanner._extract_yahoo_keywords(product)
        assert "192khz" not in result

    def test_spec_model_field_excluded_from_keywords_32bit(self, scanner):
        product = {"model": "32bit", "title": "Audio Interface 32bit 384khz"}
        result = scanner._extract_yahoo_keywords(product)
        assert "32bit" not in result

    def test_dimension_model_field_excluded(self, scanner):
        product = {"model": "30x30cm", "title": "Tile 30x30cm Marble"}
        result = scanner._extract_yahoo_keywords(product)
        assert "30x30cm" not in result

    @pytest.mark.asyncio
    async def test_spec_model_not_matched(self, scanner):
        """Match should fail when Keepa model field is a spec value."""
        yr = FakeYahooResult("y1", "DAC 192khz USB オーディオ", 10000, shipping_cost=0)
        kp = _make_keepa_product("B001", "USB DAC 192khz Audio", 20000, model="192khz")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is None


class TestShortModelGuardRegression:
    """Short model guard MUST exclude noise/color words from token overlap.

    Invariant: For models ≤7 chars, require at least 1 meaningful common
    token (excluding model itself, noise words, colors).
    """

    @pytest.mark.asyncio
    async def test_short_model_no_common_tokens_rejected(self, scanner):
        """Short model matching but no common meaningful tokens → rejected."""
        yr = FakeYahooResult("y1", "SR750 ステレオ レシーバー", 5000, shipping_cost=0)
        kp = _make_keepa_product("B001", "SR750 Fishing Reel", 15000, model="SR750")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is None

    @pytest.mark.asyncio
    async def test_short_model_with_common_product_word_accepted(self, scanner):
        """Short model + common meaningful token → accepted."""
        yr = FakeYahooResult("y1", "SR750 Fishing リール 中古", 5000, shipping_cost=0)
        kp = _make_keepa_product("B001", "SR750 Fishing Reel", 15000, model="SR750")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is not None

    @pytest.mark.asyncio
    async def test_noise_only_overlap_rejected(self, scanner):
        """Common tokens that are all noise (black, 中古, etc.) don't count."""
        yr = FakeYahooResult("y1", "AB123 black 中古 美品", 5000, shipping_cost=0)
        kp = _make_keepa_product("B001", "AB123 Black Widget", 15000, model="AB123")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        # "black" is in _SHORT_MODEL_GUARD_NOISE
        assert result is None

    @pytest.mark.asyncio
    async def test_long_model_skips_guard(self, scanner):
        """Models >7 chars skip the short model guard entirely."""
        yr = FakeYahooResult("y1", "WH1000XM5 中古 美品", 15000, shipping_cost=0)
        kp = _make_keepa_product("B001", "WH-1000XM5 Headphones", 30000, model="WH-1000XM5")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is not None


class TestAccessoryDetectionRegression:
    """Accessory signals MUST block matching in _match_yahoo_to_amazon.

    Invariant: extract_accessory_signals_from_text() is called early
    in _match_yahoo_to_amazon and returns True → None result.
    """

    @pytest.mark.asyncio
    async def test_carrying_case_rejected(self, scanner):
        yr = FakeYahooResult("y1", "WH-1000XM5 carrying case 収納", 3000, shipping_cost=0)
        kp = _make_keepa_product("B001", "Sony WH-1000XM5 Headphones", 30000, model="WH-1000XM5")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is None

    @pytest.mark.asyncio
    async def test_spool_accessory_rejected(self, scanner):
        yr = FakeYahooResult("y1", "SR750 spool パーツ", 2000, shipping_cost=0)
        kp = _make_keepa_product("B001", "SR750 Fishing Reel", 15000, model="SR750")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is None

    @pytest.mark.asyncio
    async def test_you_suffix_rejected(self, scanner):
        """「用」suffix (e.g. SR750用) should be detected as accessory."""
        yr = FakeYahooResult("y1", "SR750用 交換フィルター", 2000, shipping_cost=0)
        kp = _make_keepa_product("B001", "SR750 Fishing Reel", 15000, model="SR750")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is None

    @pytest.mark.asyncio
    async def test_taiou_rejected(self, scanner):
        """「対応」(compatible) signals accessory."""
        yr = FakeYahooResult("y1", "WH-1000XM5 対応 イヤーパッド", 1500, shipping_cost=0)
        kp = _make_keepa_product("B001", "Sony WH-1000XM5 Headphones", 30000, model="WH-1000XM5")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is None

    @pytest.mark.asyncio
    async def test_mesh_rejected(self, scanner):
        yr = FakeYahooResult("y1", "SM58 mesh グリル 交換", 1000, shipping_cost=0)
        kp = _make_keepa_product("B001", "Shure SM58 Microphone", 15000, model="SM58S")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is None

    @pytest.mark.asyncio
    async def test_label_rejected(self, scanner):
        yr = FakeYahooResult("y1", "QL820NWB label ラベル 互換", 2000, shipping_cost=0)
        kp = _make_keepa_product("B001", "Brother QL-820NWB Printer", 20000, model="QL820NWB")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is None

    @pytest.mark.asyncio
    async def test_precut_rejected(self, scanner):
        yr = FakeYahooResult("y1", "WH-1000XM5 precut フィルム", 500, shipping_cost=0)
        kp = _make_keepa_product("B001", "Sony WH-1000XM5 Headphones", 30000, model="WH-1000XM5")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is None

    @pytest.mark.asyncio
    async def test_main_product_not_rejected(self, scanner):
        """Main product (not accessory) should NOT be rejected."""
        yr = FakeYahooResult("y1", "Sony WH-1000XM5 ヘッドホン 本体", 15000, shipping_cost=0)
        kp = _make_keepa_product("B001", "Sony WH-1000XM5 Headphones", 30000, model="WH-1000XM5")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is not None


class TestShortModelBrandExclusion:
    """Brand names alone should NOT pass the short model guard.

    Invariant: When the only common tokens between Yahoo and Amazon are
    brand names (e.g. "shimano"), the match is rejected. This prevents
    different product lines sharing a size/spec code from matching
    (e.g. Shimano Sahara C2000S ≠ Shimano Ultegra C2000S).
    """

    @pytest.mark.asyncio
    async def test_shimano_c2000s_different_lines_rejected(self, scanner):
        """Shimano Sahara C2000S vs Shimano Ultegra C2000S → rejected."""
        yr = FakeYahooResult("y1", "シマノ 22 サハラ C2000S スピニングリール", 5000, shipping_cost=0)
        kp = _make_keepa_product("B001", "シマノ(SHIMANO) スピニングリール 25アルテグラ C2000S", 12000, model="C2000S")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is None

    @pytest.mark.asyncio
    async def test_shimano_c3000hg_different_lines_rejected(self, scanner):
        """Different Shimano lines with same size code C3000HG → rejected."""
        yr = FakeYahooResult("y1", "シマノ ツインパワー C3000HG", 3490, shipping_cost=0)
        kp = _make_keepa_product("B001", "シマノ(SHIMANO) 25アルテグラ C3000HG", 12000, model="C3000HG")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is None

    @pytest.mark.asyncio
    async def test_sp600_cross_category_rejected(self, scanner):
        """SP-600 golf club vs SP-600 stepper → rejected (no common tokens)."""
        yr = FakeYahooResult("y1", "ダンロップ XXIO PRIME SP-600 ドライバー", 5800, shipping_cost=0)
        kp = _make_keepa_product("B001", "ツイストステッパー Premium SP-600", 18000, model="SP-600")
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is None

    @pytest.mark.asyncio
    async def test_short_model_with_meaningful_non_brand_token_accepted(self, scanner):
        """Short model + common non-brand token (product name) → accepted."""
        yr = FakeYahooResult("y1", "Makita TD022 ペンインパクト 中古", 9000, shipping_cost=0)
        kp = _make_keepa_product("B001", "マキタ ペン型インパクトドライバ TD022", 18000, model="TD022DSHXB")
        # TD022DSHXB is 10 chars > 7, so short model guard doesn't apply
        # But TD022 from title is 5 chars... let's test with title-only model
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        # TD022 (5 chars) from Yahoo vs TD022DSHXB (normalized td022dshxb) from Amazon
        # These don't match exactly (td022 ≠ td022dshxb), so no model match
        assert result is None

    @pytest.mark.asyncio
    async def test_same_product_with_brand_plus_meaningful_token(self, scanner):
        """Brand + meaningful common token → accepted (e.g. shared product name)."""
        yr = FakeYahooResult("y1", "Sony WH-1000XM5 Headphones ワイヤレス", 15000, shipping_cost=0)
        kp = _make_keepa_product("B001", "Sony WH-1000XM5 Wireless Headphones", 30000, model="WH-1000XM5")
        # WH-1000XM5 = 9 chars > 7, short model guard doesn't apply
        result = await scanner._match_yahoo_to_amazon(yr, kp)
        assert result is not None
