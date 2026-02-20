"""Tests for extract_product_info and brand hard reject."""

from yafuama.matcher import extract_product_info, match_products


# ---------------------------------------------------------------------------
# extract_product_info tests
# ---------------------------------------------------------------------------


class TestExtractProductInfo:
    """Test brand, model number, and key token extraction from product titles."""

    def test_dyson_with_model(self):
        brand, models, tokens = extract_product_info("ダイソン Dyson V8 Slim Fluffy SV10K 掃除機")
        assert brand == "dyson"
        assert "v8" in models
        assert "sv10k" in models

    def test_iris_ohyama(self):
        brand, models, tokens = extract_product_info("アイリスオーヤマ スティッククリーナー IC-SLDCP5")
        assert brand == "iris ohyama"
        assert any("icsldcp5" in m for m in models)

    def test_twinbird(self):
        brand, models, tokens = extract_product_info("ツインバード サイクロン TC-E123")
        assert brand == "twinbird"
        assert any("tce123" in m for m in models)

    def test_sony_headphone(self):
        brand, models, tokens = extract_product_info("Sony WH-1000XM5 ヘッドホン")
        assert brand == "sony"
        assert "wh1000xm5" in models

    def test_no_brand_no_model(self):
        brand, models, tokens = extract_product_info("中古美品 送料無料 掃除機")
        assert brand is None
        assert len(models) == 0

    def test_brand_only_no_model(self):
        brand, models, tokens = extract_product_info("パナソニック 炊飯器")
        assert brand == "panasonic"
        assert len(models) == 0

    def test_makita(self):
        brand, models, tokens = extract_product_info("マキタ 充電式クリーナー CL107FD")
        assert brand == "makita"
        assert any("cl107fd" in m for m in models)

    def test_anker(self):
        brand, models, tokens = extract_product_info("Anker PowerCore 10000 A1263")
        assert brand == "anker"
        assert any("a1263" in m for m in models)

    def test_logicool(self):
        brand, models, tokens = extract_product_info("ロジクール G502 ゲーミングマウス")
        assert brand == "logicool"
        assert any("g502" in m for m in models)

    def test_balmuda(self):
        brand, models, tokens = extract_product_info("バルミューダ The Toaster K05A")
        assert brand == "balmuda"
        assert any("k05a" in m for m in models)

    def test_zojirushi(self):
        brand, models, tokens = extract_product_info("象印 NW-SA10")
        assert brand == "zojirushi"
        assert any("nwsa10" in m or "sa10" in m for m in models)

    def test_gopro(self):
        brand, models, tokens = extract_product_info("GoPro HERO12 Black")
        assert brand == "gopro"
        assert any("hero12" in m for m in models)

    def test_key_tokens_exclude_noise(self):
        """Key tokens should not contain noise words, brand, or models."""
        brand, models, tokens = extract_product_info("Sony WH-1000XM5 送料無料 ヘッドホン")
        assert brand == "sony"
        assert "sony" not in tokens
        for m in models:
            assert m not in tokens

    def test_empty_string(self):
        brand, models, tokens = extract_product_info("")
        assert brand is None
        assert len(models) == 0
        assert len(tokens) == 0


# ---------------------------------------------------------------------------
# Brand hard reject tests
# ---------------------------------------------------------------------------


class TestBrandHardReject:
    """Brand conflict should be a hard reject (is_likely_match=False)."""

    def test_iris_vs_twinbird(self):
        """Completely different brands should never match."""
        r = match_products(
            "アイリスオーヤマ 掃除機 IC-SLDCP5",
            "ツインバード サイクロン TC-E123",
        )
        assert r.brand_conflict
        assert not r.is_likely_match

    def test_dyson_vs_makita(self):
        r = match_products("ダイソン V8 掃除機", "マキタ CL107FD 掃除機")
        assert r.brand_conflict
        assert not r.is_likely_match

    def test_sony_vs_panasonic(self):
        r = match_products(
            "Sony WH-1000XM5 ヘッドホン",
            "Panasonic RP-HD600N ヘッドホン",
        )
        assert r.brand_conflict
        assert not r.is_likely_match

    def test_anker_vs_elecom(self):
        r = match_products("Anker 充電器 A2633", "エレコム 充電器 MPA-ACU08")
        assert r.brand_conflict
        assert not r.is_likely_match

    def test_same_brand_still_matches(self):
        """Same brand should NOT trigger brand_conflict."""
        r = match_products(
            "Sony WH-1000XM5 ヘッドホン",
            "Sony WH-1000XM5 Wireless Headphones",
        )
        assert not r.brand_conflict
        assert r.brand_match
        assert r.is_likely_match

    def test_brand_conflict_even_with_similar_tokens(self):
        """Even if titles share many words, different brands = no match."""
        r = match_products(
            "アイリスオーヤマ コードレス スティック 掃除機 軽量",
            "ツインバード コードレス スティック 掃除機 軽量",
        )
        assert r.brand_conflict
        assert not r.is_likely_match

    def test_no_brand_no_conflict(self):
        """When neither title has a known brand, no brand conflict."""
        r = match_products("掃除機 コードレス 軽量", "掃除機 コードレス 軽量")
        assert not r.brand_conflict


# ---------------------------------------------------------------------------
# New brand aliases recognition tests
# ---------------------------------------------------------------------------


class TestNewBrandAliases:
    """Verify all newly added brands are properly recognized."""

    def test_iris_ohyama_katakana(self):
        brand, _, _ = extract_product_info("アイリスオーヤマ 加湿器")
        assert brand == "iris ohyama"

    def test_twinbird_katakana(self):
        brand, _, _ = extract_product_info("ツインバード コーヒーメーカー")
        assert brand == "twinbird"

    def test_tiger_katakana(self):
        brand, _, _ = extract_product_info("タイガー 魔法瓶 JPC-A101")
        assert brand == "tiger"

    def test_tefal_katakana(self):
        brand, _, _ = extract_product_info("ティファール 電気ケトル")
        assert brand == "tefal"

    def test_tefal_hyphenated(self):
        brand, _, _ = extract_product_info("T-fal フライパン")
        assert brand == "tefal"

    def test_delonghi_katakana(self):
        brand, _, _ = extract_product_info("デロンギ エスプレッソマシン")
        assert brand == "delonghi"

    def test_mitsubishi_kanji(self):
        brand, _, _ = extract_product_info("三菱 エアコン MSZ-GE2524")
        assert brand == "mitsubishi"

    def test_electrolux_katakana(self):
        brand, _, _ = extract_product_info("エレクトロラックス 掃除機")
        assert brand == "electrolux"

    def test_buffalo_katakana(self):
        brand, _, _ = extract_product_info("バッファロー WiFiルーター WSR-3200")
        assert brand == "buffalo"

    def test_shure_english(self):
        brand, _, _ = extract_product_info("Shure SE846 イヤホン")
        assert brand == "shure"

    def test_hitachi_kanji(self):
        brand, _, _ = extract_product_info("日立 冷蔵庫 R-HW48R")
        assert brand == "hitachi"

    def test_toshiba_kanji(self):
        brand, _, _ = extract_product_info("東芝 洗濯機 TW-127XP3L")
        assert brand == "toshiba"

    def test_logitech_maps_to_logicool(self):
        brand, _, _ = extract_product_info("Logitech G502 HERO")
        assert brand == "logicool"
