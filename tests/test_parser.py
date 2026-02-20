"""Tests for AuctionPageParser and SearchResultsParser."""

from yafuama.scraper.parser import AuctionPageParser, SearchResultsParser


class TestAuctionPageParser:
    def setup_method(self):
        self.parser = AuctionPageParser()

    def test_parse_active_auction(self, active_html):
        result = self.parser.parse(active_html)
        assert result is not None
        assert result.auction_id == "1219987808"
        assert result.current_price == 3600
        assert result.bid_count == 5
        assert result.is_closed is False
        assert result.has_winner is False
        assert result.status == "active"
        assert result.start_time is not None
        assert result.end_time is not None
        assert result.image_url != ""
        assert "ポケカ" in result.title

    def test_parse_ended_auction(self, ended_html):
        result = self.parser.parse(ended_html)
        assert result is not None
        assert result.auction_id == "x1219674283"
        assert result.current_price == 12093
        assert result.bid_count == 17
        assert result.is_closed is True
        assert result.has_winner is False
        assert result.status == "ended_no_winner"

    def test_parse_invalid_html(self):
        result = self.parser.parse("<html><body>no data</body></html>")
        assert result is None

    def test_parse_empty_string(self):
        result = self.parser.parse("")
        assert result is None


class TestSearchResultsParser:
    def setup_method(self):
        self.parser = SearchResultsParser()

    def test_parse_search_results(self, search_html):
        results = self.parser.parse(search_html)
        assert len(results) > 0

        first = results[0]
        assert first.auction_id != ""
        assert first.title != ""
        assert first.current_price > 0
        assert first.url.startswith("https://auctions.yahoo.co.jp/")

    def test_search_result_has_seller(self, search_html):
        results = self.parser.parse(search_html)
        items_with_seller = [r for r in results if r.seller_id]
        assert len(items_with_seller) > 0

    def test_parse_empty_html(self):
        results = self.parser.parse("<html><body></body></html>")
        assert results == []
