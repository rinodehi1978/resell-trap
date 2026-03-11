"""Tests for smart product matcher."""

import pytest

from yafuama.matcher import (
    MatchResult,
    _canonicalize_tokens,
    _extract_model_numbers,
    _insert_boundary_spaces,
    _kata_to_hira,
    _split_known_brands,
    extract_accessory_signals_from_text,
    extract_model_numbers_from_text,
    is_valid_model,
    match_products,
    normalize,
    tokenize,
)


# ---------------------------------------------------------------------------
# Normalization tests
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_lowercase(self):
        assert "nintendo" in normalize("NINTENDO")

    def test_nfkc_fullwidth(self):
        # Ｎｉｎｔｅｎｄｏ (full-width) → nintendo
        result = normalize("\uff2e\uff49\uff4e\uff54\uff45\uff4e\uff44\uff4f")
        assert "nintendo" in result

    def test_katakana_to_hiragana(self):
        result = normalize("ニンテンドー")
        assert "にんてんどー" in result

    def test_boundary_spaces(self):
        # CJK followed by latin should get space inserted
        result = normalize("ソニーSony")
        tokens = result.split()
        assert len(tokens) >= 2


class TestInsertBoundarySpaces:
    def test_cjk_latin_boundary(self):
        result = _insert_boundary_spaces("あいうabc")
        assert " " in result

    def test_latin_cjk_boundary(self):
        result = _insert_boundary_spaces("abcあいう")
        assert " " in result

    def test_no_change_for_pure_latin(self):
        assert _insert_boundary_spaces("hello world") == "hello world"

    def test_no_change_for_pure_cjk(self):
        result = _insert_boundary_spaces("にんてんどー")
        assert " " not in result

    def test_empty(self):
        assert _insert_boundary_spaces("") == ""


class TestKataToHira:
    def test_basic(self):
        assert _kata_to_hira("ニンテンドー") == "にんてんどー"

    def test_mixed(self):
        result = _kata_to_hira("ソニー Sony")
        assert result.startswith("そにー")

    def test_long_vowel_mark_kept(self):
        # ー is NOT converted (it's a valid symbol in both systems)
        result = _kata_to_hira("スイッチー")
        assert "ー" in result


# ---------------------------------------------------------------------------
# Tokenization and splitting
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_basic(self):
        tokens = tokenize("hello world foo")
        assert tokens == ["hello", "world", "foo"]

    def test_delimiters(self):
        tokens = tokenize("a/b [c] (d)")
        assert tokens == ["a", "b", "c", "d"]

    def test_japanese_delimiters(self):
        tokens = tokenize("a【b】「c」")
        assert tokens == ["a", "b", "c"]


class TestSplitKnownBrands:
    def test_splits_concatenated_brand(self):
        # "にんてんどー" is a known brand alias
        tokens = _split_known_brands(["にんてんどーすいっち"])
        assert "にんてんどー" in tokens
        assert "すいっち" in tokens

    def test_no_split_for_exact_match(self):
        tokens = _split_known_brands(["nintendo"])
        assert tokens == ["nintendo"]

    def test_short_alias_splits_with_digit(self):
        # "ps" is a 2-char alias; split only if remainder is numeric
        tokens = _split_known_brands(["ps5"])
        assert "ps" in tokens
        assert "5" in tokens

    def test_short_alias_no_split_with_text(self):
        # "psu" should NOT split "ps" + "u"
        tokens = _split_known_brands(["psu"])
        assert tokens == ["psu"]


class TestCanonicalizeTokens:
    def test_brand_alias(self):
        result = _canonicalize_tokens(["にんてんどー"])
        assert result == ["nintendo"]

    def test_product_synonym(self):
        result = _canonicalize_tokens(["へっどほん"])
        assert result == ["headphone"]

    def test_unknown_token_unchanged(self):
        result = _canonicalize_tokens(["foobar123"])
        assert result == ["foobar123"]


# ---------------------------------------------------------------------------
# Model number extraction
# ---------------------------------------------------------------------------


class TestExtractModelNumbers:
    def test_alphanumeric(self):
        models = _extract_model_numbers(["wh-1000xm4", "sony", "headphone"])
        assert "wh1000xm4" in models

    def test_number_letter_short_excluded(self):
        # "3ds" is only 3 chars → excluded (min 4)
        models = _extract_model_numbers(["3ds", "nintendo"])
        assert len(models) == 0

    def test_pure_letters_excluded(self):
        models = _extract_model_numbers(["sony", "nintendo"])
        assert len(models) == 0

    def test_pure_numbers_excluded(self):
        models = _extract_model_numbers(["12345"])
        assert len(models) == 0

    def test_short_excluded(self):
        # "a1" is only 2 chars → excluded (min 5)
        models = _extract_model_numbers(["a1"])
        assert len(models) == 0

    def test_four_char_excluded(self):
        # "sv18" is only 4 chars → excluded (min 5)
        models = _extract_model_numbers(["sv18"])
        assert len(models) == 0

    def test_five_char_model_valid(self):
        # "hp04w" → exactly 5 chars, valid
        models = _extract_model_numbers(["hp04w"])
        assert "hp04w" in models

    def test_long_model_valid(self):
        models = _extract_model_numbers(["wh1000xm4"])
        assert "wh1000xm4" in models

    def test_hyphen_normalization(self):
        # Hyphens stripped: "MO-F1807-W" → "mof1807w"
        models = _extract_model_numbers(["mo-f1807-w"])
        assert "mof1807w" in models


# ---------------------------------------------------------------------------
# match_products — full integration tests
# ---------------------------------------------------------------------------


class TestMatchProducts:
    """Test real-world matching scenarios."""

    # --- TRUE MATCHES (should score high) ---

    def test_identical_titles_with_model(self):
        r = match_products("Nintendo Switch CFI-1200A 本体", "Nintendo Switch CFI-1200A 本体")
        assert r.is_likely_match
        assert r.score >= 0.40

    def test_identical_titles_no_model_rejected(self):
        """型番なし同士は is_likely_match = False（型番一致必須）"""
        r = match_products("Nintendo Switch 本体", "Nintendo Switch 本体")
        assert not r.is_likely_match  # No model number → rejected
        assert r.score >= 0.40  # Score is still high, just blocked by model requirement

    def test_case_difference(self):
        r = match_products("SONY WH-1000XM4", "Sony WH-1000XM4")
        assert r.is_likely_match

    def test_katakana_vs_english_with_model(self):
        """ニンテンドー スイッチ should match Nintendo Switch when model matches."""
        r = match_products("ニンテンドー スイッチ CFI-1200A", "Nintendo Switch CFI-1200A")
        assert r.is_likely_match
        assert r.brand_match

    def test_no_model_number_rejected(self):
        """型番なしの商品は is_likely_match = False"""
        r = match_products("ニンテンドースイッチ", "Nintendo Switch")
        assert not r.is_likely_match  # No model → rejected

    def test_model_number_match(self):
        r = match_products(
            "Sony WH-1000XM4 ワイヤレスヘッドホン",
            "Sony WH-1000XM4 Wireless Headphones",
        )
        assert r.is_likely_match
        assert r.model_match
        assert r.brand_match

    def test_fullwidth_ascii_with_model(self):
        """Full-width ASCII should normalize to half-width."""
        r = match_products("Ｓｏｎｙ　ＷＨ-1000ＸＭ4", "Sony WH-1000XM4")
        assert r.is_likely_match

    def test_ps5_abbreviation(self):
        """PS5 should match PlayStation 5."""
        r = match_products("PS5 本体 CFI-1200A", "PlayStation5 本体 CFI-1200A")
        assert r.is_likely_match
        assert r.model_match

    # --- TRUE NON-MATCHES (should score low) ---

    def test_same_brand_different_product(self):
        """Nintendo Switch 本体 vs Nintendo Switch ケース are different products."""
        r = match_products("Nintendo Switch 本体", "Nintendo Switch ケース")
        # These share brand + "switch" but differ in product type
        # Score should be borderline or below threshold
        assert r.score < 0.50

    def test_model_number_conflict(self):
        """Same brand but different model number → different product."""
        r = match_products(
            "Sony WH-1000XM4 ヘッドホン",
            "Sony WH-1000XM5 ヘッドホン",
        )
        assert r.model_conflict
        assert not r.is_likely_match

    def test_completely_different(self):
        r = match_products("Nintendo Switch 本体", "Dyson V15 掃除機")
        assert not r.is_likely_match
        assert r.score < 0.10

    def test_similar_name_different_generation(self):
        """iPhone 14 vs iPhone 15 are different products."""
        r = match_products("Apple iPhone14 ケース", "Apple iPhone15 ケース")
        assert r.model_conflict

    def test_different_model_numbers_iris_ohyama(self):
        """MO-F1807-W vs AMO-F1811-B: different model numbers → no match."""
        r = match_products(
            "アイリスオーヤマ オーブンレンジ MO-F1807-W",
            "アイリスオーヤマ オーブンレンジ AMO-F1811-B",
        )
        assert not r.is_likely_match
        assert r.model_conflict or not r.model_match

    def test_same_model_with_hyphens(self):
        """MO-F1807-W vs MOF1807W: same model, hyphens ignored → match."""
        r = match_products(
            "アイリスオーヤマ MO-F1807-W オーブンレンジ",
            "アイリスオーヤマ MOF1807W オーブンレンジ",
        )
        assert r.is_likely_match
        assert r.model_match

    # --- EDGE CASES ---

    def test_empty_titles(self):
        r = match_products("", "Nintendo Switch")
        assert not r.is_likely_match
        assert r.score == 0.0

    def test_noise_only_title(self):
        r = match_products("送料無料 美品 中古", "Nintendo Switch")
        assert not r.is_likely_match

    def test_result_has_all_fields(self):
        r = match_products("Sony WH-1000XM4", "Sony WH-1000XM4")
        assert isinstance(r, MatchResult)
        assert isinstance(r.score, float)
        assert isinstance(r.model_match, bool)
        assert isinstance(r.model_conflict, bool)
        assert isinstance(r.brand_match, bool)
        assert isinstance(r.brand_conflict, bool)
        assert isinstance(r.token_overlap, float)


# ══════════════════════════════════════════════════════════════════════
# 鉄壁の防御: Regression tests for matcher.py invariants
# ══════════════════════════════════════════════════════════════════════


class TestIsValidModelInvariant:
    """is_valid_model() is the single gate for all model number validation.

    Invariant: Every code path that accepts a model number MUST call
    is_valid_model(). This test class ensures the function itself
    correctly rejects all known false-positive patterns.
    """

    # --- Spec values ---
    @pytest.mark.parametrize("spec", [
        "32bit", "64bit", "128bit",
        "192khz", "44khz", "48khz", "96khz",
        "100mhz", "2400mhz", "5ghz",
        "128gb", "256gb", "512gb", "1024mb",
        "10000mah", "20000mah",
        "100mm", "200cm",
        "48fps", "60fps", "120fps",
        "300dpi", "600dpi",
        "3000rpm",
        "100psi", "18awg",
    ])
    def test_spec_values_rejected(self, spec):
        assert is_valid_model(spec) is False

    # --- Dimension values ---
    @pytest.mark.parametrize("dim", [
        "30x30cm", "100x200mm", "50x50m", "10x20inch",
    ])
    def test_dimension_values_rejected(self, dim):
        assert is_valid_model(dim) is False

    # --- Common word + version ---
    @pytest.mark.parametrize("word_ver", [
        "switch2", "bluetooth6", "hero13", "windows11",
        "android14", "version3", "channel5", "kindle4",
    ])
    def test_common_word_version_rejected(self, word_ver):
        assert is_valid_model(word_ver) is False

    # --- Blocklist ---
    def test_blocklisted_rejected(self):
        assert is_valid_model("52toys") is False

    # --- Too short ---
    @pytest.mark.parametrize("short", ["V8", "G5", "SV10", "3DS"])
    def test_short_rejected(self, short):
        assert is_valid_model(short) is False

    # --- No letter or no digit ---
    def test_no_digit_rejected(self):
        assert is_valid_model("Bluetooth") is False

    def test_no_letter_rejected(self):
        assert is_valid_model("12345") is False

    # --- Japanese ---
    def test_japanese_rejected(self):
        assert is_valid_model("スイッチ2") is False

    # --- Valid models MUST pass ---
    @pytest.mark.parametrize("valid", [
        "WH-1000XM5", "SV10K", "ECAM35015BH", "CFI-2000A",
        "SR750", "QL820NWB", "SV18FF", "SM58S",
    ])
    def test_valid_models_accepted(self, valid):
        assert is_valid_model(valid) is True


class TestExtractModelNumbersInvariant:
    """_extract_model_numbers uses _SPEC_UNIT_RE and _DIMENSION_RE internally.

    Invariant: Spec values and dimensions never appear in model number sets.
    """

    def test_spec_excluded_from_extraction(self):
        models = _extract_model_numbers(["192khz", "32bit", "wh1000xm5"])
        assert "192khz" not in models
        assert "32bit" not in models
        assert "wh1000xm5" in models

    def test_dimension_excluded_from_extraction(self):
        models = _extract_model_numbers(["30x30cm", "ecam35015bh"])
        assert "30x30cm" not in models
        assert "ecam35015bh" in models

    def test_extract_from_text_rejects_specs(self):
        """extract_model_numbers_from_text also filters out spec values."""
        models = extract_model_numbers_from_text("DAC USB 192khz 32bit WH-1000XM5")
        assert "192khz" not in models
        assert "32bit" not in models
        assert "wh1000xm5" in models


class TestAccessoryDetectionInvariant:
    """extract_accessory_signals_from_text must catch all accessory patterns.

    Invariant: All words in _ACCESSORY_WORDS are detected, plus
    「用」suffix detection works on compound tokens.
    """

    @pytest.mark.parametrize("text,expected", [
        ("WH-1000XM5 carrying case", True),
        ("SR750 spool パーツ", True),
        ("QL-820NWB label ラベル", True),
        ("SM58 mesh グリルボール", True),
        ("WH-1000XM5 対応 イヤーパッド", True),
        ("SR750 precut フィルム", True),
        ("SR750用 交換フィルター", True),       # 「用」suffix
        ("ECAM35015BH 互換 フィルター", True),
        ("WH-1000XM5 イヤーパッド のみ", True),
        ("WH-1000XM5 収納 ケース", True),
        # Main products should NOT trigger
        ("Sony WH-1000XM5 ヘッドホン", False),
        ("Dyson SV18FF 掃除機 コードレス", False),
    ])
    def test_accessory_detection(self, text, expected):
        assert extract_accessory_signals_from_text(text) is expected

    def test_you_suffix_on_model(self):
        """「型番用」= accessory for that model."""
        assert extract_accessory_signals_from_text("SR750用 スプール") is True

    def test_standalone_you_token(self):
        """Standalone 「用」token is in _ACCESSORY_WORDS."""
        assert extract_accessory_signals_from_text("SR750 用 フィルター") is True
