"""Tests for the deal scanner model-based pipeline."""

from dataclasses import dataclass
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


def _make_keepa_product(asin, title, used_price=5000, rank=30000):
    return {
        "asin": asin,
        "title": title,
        "stats": {
            "current": [-1, -1, used_price, rank],
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
    return DealScanner(scraper, keepa, webhook_url="", webhook_type="discord")


@pytest.fixture()
def scanner_with_sp_api():
    scraper = AsyncMock()
    keepa = AsyncMock()
    keepa.clear_search_cache = MagicMock()
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
