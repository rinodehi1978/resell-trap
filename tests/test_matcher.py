"""Tests for smart product matcher."""

from yafuama.matcher import (
    MatchResult,
    _canonicalize_tokens,
    _extract_model_numbers,
    _insert_boundary_spaces,
    _kata_to_hira,
    _split_known_brands,
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

    def test_number_letter(self):
        models = _extract_model_numbers(["3ds", "nintendo"])
        assert "3ds" in models

    def test_pure_letters_excluded(self):
        models = _extract_model_numbers(["sony", "nintendo"])
        assert len(models) == 0

    def test_pure_numbers_excluded(self):
        models = _extract_model_numbers(["12345"])
        assert len(models) == 0

    def test_short_excluded(self):
        # single char tokens excluded (len < 2 after strip)
        models = _extract_model_numbers(["a1"])
        assert "a1" in models  # len == 2, still valid


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
