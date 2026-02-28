"""Smart product matching: determines if a Yahoo Auction item is the same as an Amazon product.

Handles:
- Case differences (Nintendo / NINTENDO / nintendo)
- Full-width / half-width (Ａ→A, １→1)
- Katakana / Hiragana (ニンテンドー → にんてんどー)
- English ↔ Japanese brand names (ニンテンドー = Nintendo)
- Model number extraction and comparison (WH-1000XM4 ≠ WH-1000XM5)
- Product synonym mapping (ヘッドホン = headphone)
- Product type conflict detection (パック ≠ BOX, ケース ≠ 本体)
- Quantity mismatch detection (1個 vs 3個セット)
- Accessory vs main product detection (イヤーパッド vs ヘッドホン)
- Noise word removal (送料無料, 美品, etc.)

Scoring weights:
  Model number match      → +0.50  (strongest signal)
  Model number conflict   → -0.30  (different model = different product)
  Brand match             → +0.20
  Brand conflict          → hard reject (is_likely_match = False)
  Product type conflict   → -0.20  (different product type = different product)
  Quantity conflict        → -0.40  (different quantity = different deal)
  Accessory conflict      → -0.40  (part/accessory vs main product)
  Token Jaccard           → +0.30 * similarity

Threshold: score >= 0.40 → likely the same product
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Katakana → Hiragana
# ---------------------------------------------------------------------------

_KATA_HIRA_OFFSET = 0x60  # ord('ア') - ord('あ')


def _kata_to_hira(text: str) -> str:
    """Convert all katakana characters to hiragana."""
    result = []
    for ch in text:
        cp = ord(ch)
        if 0x30A1 <= cp <= 0x30F6:  # ァ–ヶ
            result.append(chr(cp - _KATA_HIRA_OFFSET))
        else:
            result.append(ch)
    return "".join(result)


# ---------------------------------------------------------------------------
# CJK detection and boundary splitting
# ---------------------------------------------------------------------------


def _is_cjk(ch: str) -> bool:
    """Check if a character is CJK ideograph or kana."""
    cp = ord(ch)
    return (
        0x3040 <= cp <= 0x309F      # Hiragana
        or 0x30A0 <= cp <= 0x30FF   # Katakana
        or 0x4E00 <= cp <= 0x9FFF   # CJK Unified Ideographs
        or 0x3400 <= cp <= 0x4DBF   # CJK Extension A
        or 0xFF65 <= cp <= 0xFF9F   # Halfwidth Katakana
    )


def _insert_boundary_spaces(text: str) -> str:
    """Insert spaces at CJK↔Latin/digit boundaries.

    Example: "ニンテンドーSwitch" → "ニンテンドー Switch"
    """
    if len(text) < 2:
        return text
    result = [text[0]]
    for i in range(1, len(text)):
        prev, curr = text[i - 1], text[i]
        if prev == " " or curr == " ":
            result.append(curr)
            continue
        prev_cjk = _is_cjk(prev)
        curr_cjk = _is_cjk(curr)
        if prev_cjk != curr_cjk:
            result.append(" ")
        result.append(curr)
    return "".join(result)


# ---------------------------------------------------------------------------
# Brand alias map  (normalized form → canonical English)
# ---------------------------------------------------------------------------
# Keys are in the form after NFKC + lowercase + katakana→hiragana.

_BRAND_ALIASES: dict[str, str] = {
    # --- Gaming ---
    "にんてんどー": "nintendo", "にんてんどう": "nintendo", "任天堂": "nintendo",
    "nintendo": "nintendo",
    "そにー": "sony", "sony": "sony",
    "ぷれいすてーしょん": "playstation", "ぷれすて": "playstation",
    "playstation": "playstation", "ps": "playstation",
    "xbox": "xbox",
    "まいくろそふと": "microsoft", "microsoft": "microsoft",
    "せが": "sega", "sega": "sega",
    "ばんだい": "bandai", "bandai": "bandai",
    "こなみ": "konami", "konami": "konami",
    "かぷこん": "capcom", "capcom": "capcom",
    "すくえに": "square enix", "square": "square enix",
    "なむこ": "namco", "namco": "namco",
    "たから": "takara", "takara": "takara",
    "とみー": "tomy", "tomy": "tomy",
    # --- Electronics ---
    "あっぷる": "apple", "apple": "apple",
    "さむすん": "samsung", "samsung": "samsung",
    "ぱなそにっく": "panasonic", "panasonic": "panasonic",
    "しゃーぷ": "sharp", "sharp": "sharp",
    "とうしば": "toshiba", "東芝": "toshiba", "toshiba": "toshiba",
    "ひたち": "hitachi", "日立": "hitachi", "hitachi": "hitachi",
    "きやのん": "canon", "きゃのん": "canon", "canon": "canon",
    "にこん": "nikon", "nikon": "nikon",
    "おりんぱす": "olympus", "olympus": "olympus",
    "ふじふいるむ": "fujifilm", "fujifilm": "fujifilm", "fuji": "fujifilm",
    "かしお": "casio", "casio": "casio",
    "えぷそん": "epson", "epson": "epson",
    "ぼーず": "bose", "bose": "bose",
    "jbl": "jbl",
    "ぜんはいざー": "sennheiser", "sennheiser": "sennheiser",
    "おーでぃおてくにか": "audio-technica", "audio-technica": "audio-technica",
    # --- Home / Lifestyle ---
    "だいそん": "dyson", "dyson": "dyson",
    "あいろぼっと": "irobot", "irobot": "irobot",
    "るんば": "roomba", "roomba": "roomba",
    "ぶらうん": "braun", "braun": "braun",
    "ふぃりっぷす": "philips", "philips": "philips",
    "だいきん": "daikin", "daikin": "daikin",
    "あいりすおーやま": "iris ohyama", "iris": "iris ohyama", "irisohyama": "iris ohyama",
    "ついんばーど": "twinbird", "twinbird": "twinbird",
    "まきた": "makita", "makita": "makita",
    "ぞうじるし": "zojirushi", "象印": "zojirushi", "zojirushi": "zojirushi",
    "たいがー": "tiger", "tiger": "tiger",
    "てぃふぁーる": "tefal", "tefal": "tefal", "t-fal": "tefal",
    "でろんぎ": "delonghi", "delonghi": "delonghi",
    "みつびし": "mitsubishi", "三菱": "mitsubishi", "mitsubishi": "mitsubishi",
    "えれくとろらっくす": "electrolux", "electrolux": "electrolux",
    "ばるみゅーだ": "balmuda", "balmuda": "balmuda",
    "あんかー": "anker", "anker": "anker",
    "ばっふぁろー": "buffalo", "buffalo": "buffalo",
    "えれこむ": "elecom", "elecom": "elecom",
    "ろじくーる": "logicool", "logicool": "logicool", "logitech": "logicool",
    "しゅあー": "shure", "shure": "shure",
    "ごーぷろ": "gopro", "gopro": "gopro",
    # --- Toys / Collectibles ---
    "ぽけもん": "pokemon", "pokemon": "pokemon",
    "れご": "lego", "lego": "lego",
    "めでぃこむ": "medicom", "medicom": "medicom",
    "ふぃぐま": "figma", "figma": "figma",
    "ぐっどすまいる": "goodsmile", "goodsmile": "goodsmile",
    "ことぶきや": "kotobukiya", "kotobukiya": "kotobukiya",
    "べありっく": "bearbrick", "bearbrick": "bearbrick",
}

# ---------------------------------------------------------------------------
# Product synonyms (Japanese kana → canonical)
# ---------------------------------------------------------------------------

_PRODUCT_SYNONYMS: dict[str, str] = {
    # Game consoles
    "すいっち": "switch", "switch": "switch",
    "ふぁみこん": "famicom", "famicom": "famicom",
    "すーふぁみ": "super famicom",
    "げーむぼーい": "gameboy", "gameboy": "gameboy",
    # Audio
    "へっどほん": "headphone", "headphone": "headphone", "headphones": "headphone",
    "いやほん": "earphone", "earphone": "earphone", "earphones": "earphone",
    "いやーぴーす": "earpiece",
    "すぴーかー": "speaker", "speaker": "speaker", "speakers": "speaker",
    # Accessories
    "こんとろーらー": "controller", "controller": "controller",
    "りもこん": "remote",
    "けーす": "case", "case": "case",
    "かばー": "cover", "cover": "cover",
    "ちゃーじゃー": "charger", "charger": "charger",
    "あだぷたー": "adapter", "adapter": "adapter",
    "けーぶる": "cable", "cable": "cable",
    # Devices
    "すまほ": "smartphone", "すまーとふぉん": "smartphone", "smartphone": "smartphone",
    "たぶれっと": "tablet", "tablet": "tablet",
    "のーとぱそこん": "laptop", "laptop": "laptop",
    "でぃすぷれい": "display", "display": "display",
    "もにたー": "monitor", "monitor": "monitor",
    "きーぼーど": "keyboard", "keyboard": "keyboard",
    "まうす": "mouse", "mouse": "mouse",
    "ぷりんたー": "printer", "printer": "printer",
    "かめら": "camera", "camera": "camera",
    "れんず": "lens", "lens": "lens",
    # GoPro series name
    "ひーろー": "hero", "hero": "hero",
    # Condition / edition (useful for distinguishing products)
    "でじたる": "digital", "digital": "digital",
    "わいやれす": "wireless", "wireless": "wireless",
    "ぶるーとぅーす": "bluetooth", "bluetooth": "bluetooth",
}

# ---------------------------------------------------------------------------
# Noise words to exclude from similarity comparison
# ---------------------------------------------------------------------------

_NOISE_WORDS = frozenset({
    # Japanese listing noise
    "送料", "無料", "中古", "美品", "新品", "未使用", "未開封", "即決",
    "まとめ", "じゃんく", "動作", "確認", "済み", "付き",
    "箱", "あり", "なし", "のみ", "非売品", "正規品",
    "国内", "海外", "保証", "付属", "欠品",
    # Japanese particles
    "の", "が", "で", "に", "は", "を", "と", "も", "や",
    "から", "まで", "より", "など", "ほど",
    # English noise
    "a", "the", "and", "or", "for", "with", "in", "on", "at", "to", "of",
    "is", "it", "no", "not", "be", "an", "as", "by",
    "new", "used", "free", "shipping", "japan", "import",
})

# ---------------------------------------------------------------------------
# Apparel / fashion blocklist — completely excluded from matching
# ---------------------------------------------------------------------------

_APPAREL_BRANDS = frozenset({
    # Luxury brands
    "nike", "ないき", "ナイキ",
    "adidas", "あでぃだす", "アディダス",
    "supreme", "しゅぷりーむ", "シュプリーム",
    "gucci", "ぐっち", "グッチ",
    "louis vuitton", "ルイヴィトン", "るいう゛ぃとん", "ヴィトン", "う゛ぃとん",
    "hermes", "えるめす", "エルメス",
    "chanel", "しゃねる", "シャネル",
    "prada", "ぷらだ", "プラダ",
    "dior", "でぃおーる", "ディオール",
    "balenciaga", "ばれんしあが", "バレンシアガ",
    "fendi", "ふぇんでぃ", "フェンディ",
    "burberry", "ばーばりー", "バーバリー",
    "coach", "こーち", "コーチ",
    "celine", "せりーぬ", "セリーヌ",
    "bottega veneta", "ぼってがう゛ぇねた", "ボッテガ",
    "yves saint laurent", "いう゛さんろーらん", "サンローラン",
    "loewe", "ろえべ", "ロエベ",
    "valentino", "う゛ぁれんてぃの", "ヴァレンティノ",
    "versace", "う゛ぇるさーち", "ヴェルサーチ",
    "givenchy", "じばんしー", "ジバンシー",
    # Sportswear
    "puma", "ぷーま", "プーマ",
    "reebok", "りーぼっく", "リーボック",
    "new balance", "にゅーばらんす", "ニューバランス",
    "under armour", "あんだーあーまー", "アンダーアーマー",
    "the north face", "のーすふぇいす", "ノースフェイス",
    "patagonia", "ぱたごにあ", "パタゴニア",
    "converse", "こんばーす", "コンバース",
    "vans", "ばんず", "バンズ",
    "asics", "あしっくす", "アシックス",
    # Japanese fashion
    "uniqlo", "ゆにくろ", "ユニクロ",
    "comme des garcons", "こむでぎゃるそん", "コムデギャルソン",
    "bape", "べいぷ", "ベイプ",
    "stussy", "すてゅーしー", "ステューシー",
})

_APPAREL_WORDS = frozenset({
    # Clothing
    "服", "衣類", "洋服", "ふく",
    "じゃけっと", "ジャケット", "jacket",
    "こーと", "コート", "coat",
    "ぱーかー", "パーカー", "hoodie", "parka",
    "てぃーしゃつ", "tシャツ", "tしゃつ", "t-shirt", "tshirt", "tee",
    "しゃつ", "シャツ", "shirt",
    "ぱんつ", "パンツ", "pants", "trousers",
    "じーんず", "ジーンズ", "jeans", "denim", "でにむ",
    "すかーと", "スカート", "skirt",
    "わんぴーす", "ワンピース",
    "すーつ", "スーツ", "suit",
    "べすと", "ベスト", "vest",
    "にっと", "ニット", "knit", "sweater", "せーたー",
    "すうぇっと", "スウェット", "sweatshirt",
    "ぶらうす", "ブラウス", "blouse",
    "だうん", "ダウン", "down",
    # Shoes
    "靴", "くつ", "シューズ", "しゅーず", "shoes", "shoe",
    "すにーかー", "スニーカー", "sneaker", "sneakers",
    "ぶーつ", "ブーツ", "boots",
    "さんだる", "サンダル", "sandal", "sandals",
    "ろーふぁー", "ローファー", "loafer",
    "ぱんぷす", "パンプス", "pumps",
    # Bags
    "ばっぐ", "バッグ", "bag", "bags",
    "はんどばっぐ", "ハンドバッグ", "handbag",
    "しょるだーばっぐ", "ショルダーバッグ",
    "とーとばっぐ", "トートバッグ", "tote",
    "りゅっく", "リュック", "backpack",
    "ぼすとん", "ボストン",
    "くらっち", "クラッチ", "clutch",
    # Wallets / accessories
    "財布", "さいふ", "wallet",
    "長財布", "ながさいふ",
    "折り財布", "おりさいふ",
    "がまぐち", "がま口",
    "きーけーす", "キーケース",
    "かーどけーす", "カードケース",
    "めいしいれ", "名刺入れ",
    "こいんけーす", "コインケース",
    # Belts / scarves / ties
    "べると", "ベルト", "belt",
    "すかーふ", "スカーフ", "scarf",
    "ねくたい", "ネクタイ", "necktie", "tie",
    "まふらー", "マフラー", "muffler",
    "すとーる", "ストール", "stole",
    # Hats
    "帽子", "ぼうし", "hat", "cap",
    "きゃっぷ", "びーにー", "ビーニー", "beanie",
    # Jewelry / accessories
    "あくせさりー", "アクセサリー", "accessory",
    "ねっくれす", "ネックレス", "necklace",
    "ぶれすれっと", "ブレスレット", "bracelet",
    "りんぐ", "リング", "ring",
    "ぴあす", "ピアス", "piercing", "earring",
    "いやりんぐ", "イヤリング",
    "さんぐらす", "サングラス", "sunglasses",
    # Apparel general
    "apparel", "あぱれる", "アパレル",
    "fashion", "ふぁっしょん", "ファッション",
    "wear", "うぇあ", "ウェア",
    "clothing", "くろーじんぐ",
})


def is_apparel(text: str) -> bool:
    """Check if text is apparel-related (brand or product type).

    Works on raw (un-normalized) text so it can be used early in the pipeline.
    """
    lower = text.lower()
    normalized = normalize(text)
    tokens = set(tokenize(normalized))

    # Check apparel brands (raw text match)
    for brand in _APPAREL_BRANDS:
        if brand in lower or brand in normalized:
            return True

    # Check apparel product words (token match)
    if tokens & _APPAREL_WORDS:
        return True

    return False


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def extract_product_info(title: str) -> tuple[str | None, set[str], list[str]]:
    """Extract brand, model numbers, and key tokens from a product title.

    Returns:
        (brand, model_numbers, key_tokens)
        - brand: canonical brand name or None
        - model_numbers: set of model number strings (e.g. {"sv18", "v12"})
        - key_tokens: meaningful tokens excluding noise, models, and brand
    """
    norm = normalize(title)
    tokens = tokenize(norm)
    tokens = _split_known_brands(tokens)
    canon = _canonicalize_tokens(tokens)
    canon = _merge_product_number_tokens(canon)
    brand = _extract_brand(canon)
    models = _extract_model_numbers(canon)
    key_tokens = [
        t for t in canon
        if t not in _NOISE_WORDS and len(t) >= 2
        and t not in models and t != brand
    ]
    return brand, models, key_tokens


def extract_model_numbers_from_text(text: str) -> set[str]:
    """Extract model numbers from arbitrary text (descriptions, features, etc.)."""
    norm = normalize(text)
    tokens = tokenize(norm)
    tokens = _split_known_brands(tokens)
    canon = _canonicalize_tokens(tokens)
    canon = _merge_product_number_tokens(canon)
    return _extract_model_numbers(canon)


def extract_accessory_signals_from_text(text: str) -> bool:
    """Check if arbitrary text contains accessory/parts language."""
    norm = normalize(text)
    tokens = tokenize(norm)
    tokens = _split_known_brands(tokens)
    canon = _canonicalize_tokens(tokens)
    return _has_accessory_words(canon)


def normalize(text: str) -> str:
    """Normalize text: NFKC → lowercase → katakana→hiragana → boundary spaces."""
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    text = _kata_to_hira(text)
    text = _insert_boundary_spaces(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def tokenize(text: str) -> list[str]:
    """Split normalized text into tokens on whitespace and delimiters."""
    raw = re.split(r"[\s/\[\]\(\)（）【】「」『』、。,\.]+", text)
    return [t for t in raw if t]


def _split_known_brands(tokens: list[str]) -> list[str]:
    """Split tokens that start with a known brand name.

    Example: "にんてんどーすいっち" → ["にんてんどー", "すいっち"]
    Short aliases (len < 3, e.g. "ps") only split if remainder is numeric.
    """
    # Sort longest-first so we match the most specific alias
    sorted_aliases = sorted(_BRAND_ALIASES.keys(), key=len, reverse=True)

    result = []
    for token in tokens:
        found = False
        for alias in sorted_aliases:
            if len(alias) < 2:
                continue
            if token == alias:
                break  # Exact match — no split needed
            if token.startswith(alias) and len(token) > len(alias):
                remainder = token[len(alias):]
                # Short aliases: only split if remainder is purely numeric
                if len(alias) < 3 and not remainder.isdigit():
                    continue
                result.append(alias)
                result.append(remainder)
                found = True
                break
        if not found:
            result.append(token)
    return result


def _canonicalize_tokens(tokens: list[str]) -> list[str]:
    """Replace known brand names and product synonyms with canonical forms."""
    result = []
    for t in tokens:
        canon = _BRAND_ALIASES.get(t)
        if canon:
            result.append(canon)
            continue
        canon = _PRODUCT_SYNONYMS.get(t)
        if canon:
            result.append(canon)
            continue
        result.append(t)
    return result


_SPEC_UNIT_RE = re.compile(
    r"^\d+(?:mah|mhz|ghz|gb|tb|mb|hz|mm|cm|kg|mp|db|lm|ch|k|w|v)$"
)


# Product-line names that should merge with an adjacent number token
# to form a model number (e.g. "hero" + "12" → "hero12")
_MERGE_PREFIX_WORDS = frozenset({"hero"})


def _merge_product_number_tokens(tokens: list[str]) -> list[str]:
    """Merge product line names with adjacent number tokens.

    Example: ["gopro", "hero", "12", "black"] → ["gopro", "hero12", "black"]
    Handles GoPro Hero series where katakana "ヒーロー12" splits into
    "hero" + "12" after synonym replacement.
    """
    result: list[str] = []
    i = 0
    while i < len(tokens):
        if (
            i + 1 < len(tokens)
            and tokens[i] in _MERGE_PREFIX_WORDS
            and tokens[i + 1].isdigit()
        ):
            result.append(tokens[i] + tokens[i + 1])
            i += 2
        else:
            result.append(tokens[i])
            i += 1
    return result


def _extract_model_numbers(tokens: list[str]) -> set[str]:
    """Extract tokens that look like model numbers (contain both letters and digits).

    Examples: "wh-1000xm4", "cfi-1200a", "ps5", "3ds", "rtx3080"
    Excludes spec/unit tokens like "4k", "1ch", "128gb", "60hz".
    """
    models = set()
    for t in tokens:
        # Strip hyphens and long-vowel marks for the check
        stripped = re.sub(r"[-ー]", "", t)
        has_letter = bool(re.search(r"[a-z]", stripped))
        has_digit = bool(re.search(r"[0-9]", stripped))
        if has_letter and has_digit and len(stripped) >= 2:
            if _SPEC_UNIT_RE.match(stripped):
                continue  # Skip spec/unit tokens (4k, 1ch, 128gb, etc.)
            models.add(stripped)
    return models


def _models_color_suffix_match(models_a: set[str], models_b: set[str]) -> bool:
    """Check if models match after ignoring color code suffixes.

    Handles cases like HP04 vs HP04IBN, SV18 vs SV18FF, TP07 vs TP07WS
    where the suffix is a 2+ letter color/SKU code appended to the base model.

    Rules:
    - One model must be a prefix of the other
    - The remaining suffix must be purely alphabetical (no digits)
    - Suffix must be 2+ characters (single-letter suffixes like 'K' may be variants)
    """
    for a in models_a:
        for b in models_b:
            if a == b:
                continue
            if len(b) > len(a) and b.startswith(a):
                suffix = b[len(a):]
                if suffix.isalpha() and len(suffix) >= 2:
                    return True
            if len(a) > len(b) and a.startswith(b):
                suffix = a[len(b):]
                if suffix.isalpha() and len(suffix) >= 2:
                    return True
    return False


def _extract_brand(tokens: list[str]) -> str | None:
    """Find the first known brand among canonicalized tokens."""
    for t in tokens:
        if t in _BRAND_ALIASES.values():
            return t
    return None


# Regex to extract the letter prefix of a model number (e.g. "sv" from "sv10k")
_MODEL_PREFIX_RE = re.compile(r"^([a-z]+)")

# Known prefix pairs: series name + model code for one product
# e.g. Dyson "V8" + "SV10K" → same product
_PAIRED_PREFIXES = [{"v", "sv"}, {"cf", "cfi"}, {"eh", "er"}, {"hero", "chdhx"}]


def _count_model_families(models: set[str]) -> int:
    """Count distinct model families from a set of model numbers.

    "V8 + SV10K" = 1 family (paired series+code).
    "V7 + V8" = 2 families (different products with same prefix).
    """
    if len(models) <= 1:
        return len(models)

    # Extract prefix for each model
    prefix_map: dict[str, str] = {}  # model → prefix
    for m in models:
        match = _MODEL_PREFIX_RE.match(m)
        prefix_map[m] = match.group(1) if match else m

    # Try to pair models with known prefix pairs, remove paired ones
    remaining = set(models)
    pairs_found = 0
    for pair in _PAIRED_PREFIXES:
        by_prefix: dict[str, list[str]] = {}
        for m in remaining:
            if prefix_map[m] in pair:
                by_prefix.setdefault(prefix_map[m], []).append(m)
        # A valid pair: exactly 1 model from each of 2 different prefixes
        if len(by_prefix) == 2 and all(len(v) == 1 for v in by_prefix.values()):
            pairs_found += 1
            for ms in by_prefix.values():
                remaining.discard(ms[0])

    return pairs_found + len(remaining)


# ---------------------------------------------------------------------------
# Product-type groups — tokens within a group are interchangeable;
# tokens across groups are *conflicting* (パック ≠ BOX ≠ 本体 ≠ ケース).
# After normalization/canonicalization, these are matched in lowercase hiragana.
# ---------------------------------------------------------------------------

_PRODUCT_TYPE_GROUPS: list[frozenset[str]] = [
    frozenset({"本体", "ほんたい"}),
    frozenset({"けーす", "case", "かばー", "cover"}),
    frozenset({"ぱっく", "pack"}),
    frozenset({"box", "ぼっくす"}),
    frozenset({"せっと", "set"}),
    frozenset({"ばんどる", "bundle"}),
    frozenset({"りふぃる", "refill", "かえ", "替え"}),
    frozenset({"こんとろーらー", "controller"}),
    frozenset({"充電", "じゅうでん", "charger"}),
    frozenset({"拡張", "かくちょう", "expansion"}),
    frozenset({"ぷろも", "promo", "promotional"}),
    frozenset({"すたーたー", "starter"}),
    frozenset({"ぶーすたー", "booster"}),
]

# Build a lookup: token → group index
_TYPE_TOKEN_TO_GROUP: dict[str, int] = {}
for _gi, _group in enumerate(_PRODUCT_TYPE_GROUPS):
    for _tok in _group:
        _TYPE_TOKEN_TO_GROUP[_tok] = _gi


def _extract_product_types(tokens: list[str]) -> set[int]:
    """Return the set of product-type group indices found in the token list."""
    groups: set[int] = set()
    for t in tokens:
        gi = _TYPE_TOKEN_TO_GROUP.get(t)
        if gi is not None:
            groups.add(gi)
    return groups


# ---------------------------------------------------------------------------
# Sub-model variant words — these modify a base model to identify a
# distinct product variant.  When one title has them and the other doesn't,
# they are likely different products.
# (e.g. "V8 Fluffy" vs "V8 Slim Fluffy Extra")
# ---------------------------------------------------------------------------

_SUBMODEL_WORDS = frozenset({
    # Generic sub-model variants
    "slim", "すりむ",
    "extra", "えくすとら",
    "plus", "ぷらす",
    "pro", "ぷろ",
    "lite", "らいと",
    "mini", "みに",
    "max", "まっくす",
    "ultra", "うるとら",
    "neo", "ねお",
    "advance", "あどばんす",
    "premium", "ぷれみあむ",
    "deluxe", "でらっくす",
    "compact", "こんぱくと",
    "standard", "すたんだーど",
    # Dyson cordless vacuum variants (V6-V15)
    "fluffy", "ふらっふぃ",
    "absolute", "あぶそりゅーと",
    "animal", "あにまる",
    "motorhead", "もーたーへっど",
    "origin", "おりじん",
    "complete", "こんぷりーと",
    "totalclean",
    # Dyson hair products
    "supersonic", "すーぱーそにっく",
    "airwrap", "えあらっぷ",
    "corrale", "こらーる",
    # GoPro edition variants
    "creator", "くりえいたー",
    "session", "せっしょん",
})

# Map katakana submodel words → canonical English form for comparison
_SUBMODEL_CANONICAL: dict[str, str] = {
    "すりむ": "slim", "えくすとら": "extra", "ぷらす": "plus",
    "ぷろ": "pro", "らいと": "lite", "みに": "mini",
    "まっくす": "max", "うるとら": "ultra", "ねお": "neo",
    "あどばんす": "advance", "ぷれみあむ": "premium",
    "でらっくす": "deluxe", "こんぱくと": "compact",
    "すたんだーど": "standard",
    "ふらっふぃ": "fluffy", "あぶそりゅーと": "absolute",
    "あにまる": "animal", "もーたーへっど": "motorhead",
    "おりじん": "origin", "こんぷりーと": "complete",
    "すーぱーそにっく": "supersonic", "えあらっぷ": "airwrap",
    "こらーる": "corrale",
    "くりえいたー": "creator", "せっしょん": "session",
}


def _extract_submodel_hits(tokens: list[str]) -> set[str]:
    """Extract submodel words from tokens, with substring matching for
    concatenated katakana (e.g. "くりえいたーえでぃしょん" contains "くりえいたー").

    Also checks adjacent token pairs for compound words (e.g. "total"+"clean" → "totalclean").
    Returns canonical (English) forms so "くりえいたー" and "creator" match.
    """
    found: set[str] = set()
    for t in tokens:
        if t in _SUBMODEL_WORDS:
            found.add(_SUBMODEL_CANONICAL.get(t, t))
        elif len(t) >= 6:
            for sw in _SUBMODEL_WORDS:
                if len(sw) >= 4 and sw in t:
                    found.add(_SUBMODEL_CANONICAL.get(sw, sw))
    # Check adjacent token pairs for compound submodel words
    for i in range(len(tokens) - 1):
        combined = tokens[i] + tokens[i + 1]
        if combined in _SUBMODEL_WORDS:
            found.add(_SUBMODEL_CANONICAL.get(combined, combined))
    return found


def _submodel_conflict(y_tokens: list[str], a_tokens: list[str]) -> bool:
    """Check if sub-model variant words differ between the two titles.

    Only triggers when at least one side has model numbers (to avoid
    false positives on generic titles).
    """
    y_sub = _extract_submodel_hits(y_tokens)
    a_sub = _extract_submodel_hits(a_tokens)
    # Only flag conflict when BOTH sides have submodel words but different.
    # One side omitting the variant name (y_sub empty) is not a conflict
    # — the listing simply doesn't mention the variant.
    if not y_sub or not a_sub:
        return False
    return y_sub != a_sub


# ---------------------------------------------------------------------------
# Accessory / parts detection — if one title has accessory words and the
# other does not, the items are almost certainly different products
# (e.g. "WH-1000XM5 イヤーパッド" vs "WH-1000XM5 ヘッドホン").
# ---------------------------------------------------------------------------

_ACCESSORY_WORDS = frozenset({
    # Pads / cushions
    "ぱっど", "pad", "いやーぱっど", "くっしょん", "cushion",
    # Adapters / mounts
    "あだぷたー", "adapter", "まうんと", "mount", "こんばーたー", "converter",
    # Cables / connectors
    "けーぶる", "cable", "cord", "こーど", "こねくたー", "connector",
    # Covers / protectors
    "ふぃるむ", "film", "ぷろてくたー", "protector", "がーど", "guard",
    # Batteries / power / chargers
    "ばってりー", "battery", "でんち", "電池",
    "充電器", "じゅうでんき", "充電", "じゅうでん",
    "acあだぷたー", "電源", "でんげん",
    # Replacement / spare
    "交換", "こうかん", "替え", "かえ", "すぺあ", "spare",
    "部品", "ぶひん", "ぱーつ", "parts", "part",
    # Straps / holders
    "すとらっぷ", "strap", "ほるだー", "holder", "くりっぷ", "clip",
    # Caps / tips
    "きゃっぷ", "cap", "ちっぷ", "tip", "のずる", "nozzle",
    # Filters
    "ふぃるたー", "filter",
    # Stands / docks
    "すたんど", "stand", "どっく", "dock", "くれーどる", "cradle",
    # Bags / pouches
    "ぽーち", "pouch",
    # Ink / toner (printers)
    "いんく", "ink", "となー", "toner", "りぼん", "ribbon",
    # Brush / roller (vacuums)
    "ぶらし", "brush", "ろーらー", "roller", "へっど", "head",
    # Remote
    "りもこん", "remote",
    # Housing / case (action cameras)
    "はうじんぐ", "housing", "防水ケース", "ぼうすいけーす",
    # Mods / modules (GoPro etc.)
    "mod", "もっど", "もじゅーる", "module",
    # Selfie stick / tripod
    "自撮り棒", "じどりぼう", "せるふぃーすてぃっく",
    "三脚", "さんきゃく", "tripod",
    # Only / sole (signals partial item)
    "のみ", "only", "単品", "たんぴん", "単体", "たんたい",
})

# Words that indicate a main/complete product (not an accessory)
_MAIN_PRODUCT_WORDS = frozenset({
    "本体", "ほんたい", "body",
    "せっと", "set", "ふるせっと",
    "わいやれす", "wireless", "ぶるーとぅーす", "bluetooth",
    "こーどれす", "cordless",
})


# Suffixes that confirm a prefix-matched token is an accessory/part
# e.g. "へっど軽量版" → "へっど" + "軽量版" (版 is a confirming suffix)
_ACCESSORY_PREFIX_SUFFIXES = frozenset({
    "版", "用", "部", "型", "式", "台", "器",
    "のみ", "単体", "単品", "交換", "替え",
    "ぱーつ", "きっと", "kit",
})


def _has_accessory_words(tokens: list[str]) -> bool:
    """Check if token list contains words indicating a part/accessory.

    Uses exact match, suffix match, AND guarded prefix match to catch:
    - "電源あだぷたー" → ends with "あだぷたー" (accessory word)
    - "へっど軽量版"   → starts with "へっど" + remainder has confirming suffix
    """
    token_set = set(tokens)
    # Fast path: exact match
    if token_set & _ACCESSORY_WORDS:
        return True
    # Dynamic learned accessory words
    try:
        from .matcher_overrides import overrides
        if token_set & overrides.extra_accessory_words:
            return True
    except ImportError:
        pass
    # Suffix + guarded prefix match for compounds
    for t in tokens:
        if len(t) < 4:
            continue
        for aw in _ACCESSORY_WORDS:
            if len(aw) >= 3 and t != aw:
                # Suffix match (safe — "電源あだぷたー")
                if t.endswith(aw):
                    return True
                # Prefix match (needs confirmation to avoid "こーどれすくりーなー")
                if t.startswith(aw):
                    remainder = t[len(aw):]
                    # Short remainder (1-2 chars like 版/用/器) → accessory
                    if len(remainder) <= 2:
                        return True
                    # Check for confirming suffixes
                    for sfx in _ACCESSORY_PREFIX_SUFFIXES:
                        if remainder.endswith(sfx):
                            return True
    return False


def _accessory_in_leading_tokens(tokens: list[str], max_pos: int = 5) -> bool:
    """Check if accessory words appear in the first meaningful tokens.

    Titles like "充電アダプター 充電器 205720-04 掃除機..." have the
    real product type at the start.  Yahoo sellers put 検索用 filler at
    the end, so leading tokens are the strongest signal.
    """
    meaningful = [
        t for t in tokens[:max_pos * 2]       # look in first ~10 raw tokens
        if t not in _NOISE_WORDS and len(t) >= 2
    ][:max_pos]                                # keep first 5 meaningful
    return _has_accessory_words(meaningful)


def _has_main_product_words(tokens: list[str]) -> bool:
    """Check if token list contains words indicating a complete/main product."""
    return bool(set(tokens) & _MAIN_PRODUCT_WORDS)


# ---------------------------------------------------------------------------
# Quantity extraction — detect "3個セット", "2-pack", "×5", etc.
# ---------------------------------------------------------------------------

# Pattern: number + Japanese counter/set word
_QTY_JA_RE = re.compile(
    r"(\d+)\s*(?:個|本|枚|箱|袋|缶|足|台|丁|組|点|巻)"
    r"(?:せっと|set|いり|入り|入|ぱっく|pack)?",
)
# Pattern: number + set/pack (English)
_QTY_EN_RE = re.compile(
    r"(\d+)\s*[-]?\s*(?:pack|pcs|pieces|set|count)\b"
    r"|(?:set\s+of|pack\s+of)\s+(\d+)"
    r"|[x×]\s*(\d+)\b",
    re.IGNORECASE,
)
# Pattern: Japanese "Nこせっと" style (already normalized to hiragana)
_QTY_JA_SET_RE = re.compile(r"(\d+)\s*こ?\s*せっと")


def _extract_quantity(text: str) -> int:
    """Extract product quantity from normalized text. Returns 1 if not found."""
    # Japanese patterns
    for m in _QTY_JA_RE.finditer(text):
        qty = int(m.group(1))
        if 2 <= qty <= 100:
            return qty
    for m in _QTY_JA_SET_RE.finditer(text):
        qty = int(m.group(1))
        if 2 <= qty <= 100:
            return qty
    # English patterns
    for m in _QTY_EN_RE.finditer(text):
        qty_str = m.group(1) or m.group(2) or m.group(3)
        if qty_str:
            qty = int(qty_str)
            if 2 <= qty <= 100:
                return qty
    return 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

MATCH_THRESHOLD = 0.40
STRICT_MATCH_THRESHOLD = 0.55  # For high-margin deals (50%+)


@dataclass
class MatchResult:
    """Result of comparing two product titles."""

    score: float            # 0.0 – 1.0
    model_match: bool       # Model numbers found and matched
    model_conflict: bool    # Model numbers found but different
    brand_match: bool       # Known brands matched
    brand_conflict: bool    # Known brands found but different
    type_conflict: bool     # Product types found but different (パック vs BOX)
    qty_conflict: bool      # Quantities differ (1個 vs 3個セット)
    accessory_conflict: bool  # One is accessory/part, other is main product
    token_overlap: float    # Jaccard similarity of meaningful tokens
    keepa_model_match: bool = False  # Keepa model field also confirms model match

    @property
    def is_likely_match(self) -> bool:
        if self.qty_conflict:
            return False  # Hard reject: different quantity
        if self.brand_conflict:
            return False  # Hard reject: different brand = different product
        if self.model_conflict:
            return False  # Hard reject: both have model numbers but different
        if self.accessory_conflict:
            return False  # Hard reject: one is accessory/part, other is main
        # 型番一致 → マッチ（最強シグナル、スコア閾値スキップ）
        if self.model_match or self.keepa_model_match:
            return True
        # 片方/両方に型番なし → 従来のスコア制にフォールバック
        threshold = MATCH_THRESHOLD
        try:
            from .matcher_overrides import overrides
            threshold += overrides.threshold_adjustment
        except ImportError:
            pass
        return self.score >= threshold

    def passes_strict_check(self) -> bool:
        """Stricter validation for high-margin deals (50%+).

        High margins usually mean mismatched products. Require:
        - Higher score threshold (0.55 vs 0.40)
        - Must have model number match OR high token overlap
        - No model conflict, no type conflict
        """
        if self.qty_conflict:
            return False
        if self.model_conflict or self.type_conflict:
            return False
        if self.score < STRICT_MATCH_THRESHOLD:
            return False
        if not self.model_match and self.token_overlap < 0.40:
            return False
        return True


def match_products(yahoo_title: str, amazon_title: str) -> MatchResult:
    """Compare a Yahoo Auction title with an Amazon product title.

    Returns a MatchResult with a score between 0.0 and 1.0 and
    diagnostic flags.  Use ``result.is_likely_match`` (threshold 0.35)
    for a boolean decision.
    """
    # 1. Normalize
    y_norm = normalize(yahoo_title)
    a_norm = normalize(amazon_title)

    # 2. Tokenize
    y_tokens = tokenize(y_norm)
    a_tokens = tokenize(a_norm)

    if not y_tokens or not a_tokens:
        return MatchResult(0.0, False, False, False, False, False, False, False, 0.0)

    # 3. Split concatenated brand names
    y_tokens = _split_known_brands(y_tokens)
    a_tokens = _split_known_brands(a_tokens)

    # 4. Canonicalize (brand + product synonyms)
    y_canon = _canonicalize_tokens(y_tokens)
    a_canon = _canonicalize_tokens(a_tokens)

    # 4.5. Merge product line names with adjacent numbers (e.g. "hero"+"12" → "hero12")
    y_canon = _merge_product_number_tokens(y_canon)
    a_canon = _merge_product_number_tokens(a_canon)

    score = 0.0

    # --- Model number comparison (strongest signal) ---
    y_models = _extract_model_numbers(y_canon)
    a_models = _extract_model_numbers(a_canon)
    model_match = False
    model_conflict = False

    if y_models and a_models:
        if y_models & a_models:
            model_match = True
            score += 0.50
        elif _models_color_suffix_match(y_models, a_models):
            # e.g. HP04 vs HP04IBN, SV18FF vs SV18 — color code suffix only
            model_match = True
            score += 0.50
        else:
            # Both have model numbers but none match → very likely different products
            model_conflict = True
            score -= 0.30

    # --- Brand comparison ---
    y_brand = _extract_brand(y_canon)
    a_brand = _extract_brand(a_canon)
    brand_match = False
    brand_conflict = False

    if y_brand and a_brand:
        if y_brand == a_brand:
            brand_match = True
            score += 0.20
        else:
            brand_conflict = True
            score -= 0.10

    # --- Product type conflict (パック vs BOX, ケース vs 本体, etc.) ---
    y_types = _extract_product_types(y_canon)
    a_types = _extract_product_types(a_canon)
    type_conflict = False

    if y_types and a_types and not (y_types & a_types):
        # Both have product type tokens but they belong to different groups
        type_conflict = True
        score -= 0.20

    # --- Multi-model = universal accessory detection ---
    # A title with 2+ distinct model *families* is "V7/V8用" = universal part
    # But "V8 SV10K" is one product (series name + model code), not two.
    y_multi_model = _count_model_families(y_models) >= 2
    a_multi_model = _count_model_families(a_models) >= 2

    # --- "用" (for/compatible with) detection ---
    # "V11用ローラーヘッド" → "V11" + "用..." = accessory "for V11"
    y_has_you = any(t.startswith("用") or t.endswith("用") for t in y_canon)
    a_has_you = any(t.startswith("用") or t.endswith("用") for t in a_canon)

    # --- Accessory vs main product conflict ---
    y_is_accessory = _has_accessory_words(y_canon) or y_multi_model or y_has_you
    a_is_accessory = _has_accessory_words(a_canon) or a_multi_model or a_has_you
    accessory_conflict = False

    if y_is_accessory != a_is_accessory:
        # One is an accessory/part, the other is not → different product
        accessory_conflict = True
        # Stronger penalty if accessory word is in leading tokens (title start)
        if _accessory_in_leading_tokens(y_canon) or _accessory_in_leading_tokens(a_canon):
            score -= 0.60
        else:
            score -= 0.40

    # --- Sub-model variant conflict (Slim vs non-Slim, Extra vs non-Extra) ---
    if model_match and _submodel_conflict(y_canon, a_canon):
        # Same base model but different variant → different product
        model_match = False
        model_conflict = True
        score -= 0.50  # Reverse the model match bonus and add penalty

    # --- Quantity conflict (1個 vs 3個セット) ---
    y_qty = _extract_quantity(y_norm)
    a_qty = _extract_quantity(a_norm)
    qty_conflict = y_qty != a_qty

    if qty_conflict:
        score -= 0.40  # Very strong penalty — different quantity = different deal

    # --- Token Jaccard similarity (excluding noise) ---
    y_clean = {t for t in y_canon if t not in _NOISE_WORDS and len(t) >= 2}
    a_clean = {t for t in a_canon if t not in _NOISE_WORDS and len(t) >= 2}
    jaccard = 0.0

    if y_clean and a_clean:
        intersection = len(y_clean & a_clean)
        union = len(y_clean | a_clean)
        jaccard = intersection / union if union else 0.0
        score += 0.30 * jaccard

    final = max(0.0, min(1.0, score))

    return MatchResult(
        score=final,
        model_match=model_match,
        model_conflict=model_conflict,
        brand_match=brand_match,
        brand_conflict=brand_conflict,
        type_conflict=type_conflict,
        qty_conflict=qty_conflict,
        accessory_conflict=accessory_conflict,
        token_overlap=jaccard,
    )


def keywords_are_similar(kw1: str, kw2: str, threshold: float = 0.6) -> bool:
    """Check if two search keywords are similar enough to be considered duplicates.

    Three-layer check:
    0. Brand conflict → never similar ("sony X" vs "dyson X")
    1. Token-level Jaccard (catches "Sony ヘッドホン" vs "ソニー ヘッドホン")
    2. Character-level overlap (catches compound token differences like
       "サイクロン式" vs "サイクロン掃除機" where tokens don't split)
    """
    n1 = normalize(kw1)
    n2 = normalize(kw2)

    t1 = set(_canonicalize_tokens(
        _split_known_brands(tokenize(n1))
    ))
    t2 = set(_canonicalize_tokens(
        _split_known_brands(tokenize(n2))
    ))
    # Remove noise and short tokens
    t1 = {t for t in t1 if t not in _NOISE_WORDS and len(t) >= 2}
    t2 = {t for t in t2 if t not in _NOISE_WORDS and len(t) >= 2}
    if not t1 or not t2:
        return False

    # Layer 0: Brand/model guard → different search intent
    brand_values = set(_BRAND_ALIASES.values())
    b1 = t1 & brand_values
    b2 = t2 & brand_values
    # Different brands → never similar ("sony X" vs "dyson X")
    if b1 and b2 and b1 != b2:
        return False
    # One has a brand, the other doesn't → different intent
    if bool(b1) != bool(b2):
        return False

    # Model number conflict → "X v8" vs "X v10" are different
    m1 = _extract_model_numbers(list(t1))
    m2 = _extract_model_numbers(list(t2))
    if m1 and m2 and not (m1 & m2):
        return False

    # Layer 1: Token Jaccard
    jaccard = len(t1 & t2) / len(t1 | t2)
    if jaccard >= threshold:
        return True

    # Layer 2: Character-level overlap for short keywords (2-4 tokens)
    # Handles compound tokens like "さいくろん式" vs "さいくろん掃除機"
    if len(t1) <= 4 and len(t2) <= 4:
        # Strip spaces and compare character bigrams
        s1 = n1.replace(" ", "")
        s2 = n2.replace(" ", "")
        if len(s1) < 4 or len(s2) < 4:
            return False
        bg1 = {s1[i:i+2] for i in range(len(s1) - 1)}
        bg2 = {s2[i:i+2] for i in range(len(s2) - 1)}
        bigram_sim = len(bg1 & bg2) / len(bg1 | bg2) if bg1 | bg2 else 0
        if bigram_sim >= 0.6:
            return True

    return False
