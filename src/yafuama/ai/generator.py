"""Keyword candidate generator using multiple discovery strategies."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..config import settings
from ..matcher import extract_model_numbers_from_text, extract_product_info, is_apparel
from ..models import DealAlert, KeywordCandidate, WatchedKeyword
from .analyzer import KNOWN_BRANDS, KeywordInsights

logger = logging.getLogger(__name__)

# English ↔ Katakana mapping for synonym generation
SYNONYM_MAP = {
    "switch": "スイッチ",
    "card": "カード",
    "game": "ゲーム",
    "controller": "コントローラー",
    "camera": "カメラ",
    "headphone": "ヘッドホン",
    "speaker": "スピーカー",
    "figure": "フィギュア",
    "model": "モデル",
    "watch": "ウォッチ",
    "tablet": "タブレット",
    "printer": "プリンター",
    "lens": "レンズ",
    "monitor": "モニター",
    "keyboard": "キーボード",
    "mouse": "マウス",
    "router": "ルーター",
    "drone": "ドローン",
    "robot": "ロボット",
}

# Common abbreviation expansions
ABBREVIATION_MAP = {
    "ps5": "PlayStation 5",
    "ps4": "PlayStation 4",
    "ps3": "PlayStation 3",
    "ps2": "PlayStation 2",
    "3ds": "ニンテンドー3DS",
    "ds": "ニンテンドーDS",
    "gc": "ゲームキューブ",
    "sfc": "スーパーファミコン",
    "fc": "ファミコン",
    "gb": "ゲームボーイ",
    "gba": "ゲームボーイアドバンス",
}

# Condition / packaging variants
CONDITION_SUFFIXES = ["中古", "ジャンク", "BOX", "セット", "本体", "限定"]

# Low-quality tokens to exclude from keyword generation
# These are common in listing titles but have no product specificity
_LOW_QUALITY_TOKENS = frozenset({
    # Color names
    "ブラック", "黒", "白", "ホワイト", "シルバー", "ゴールド", "レッド", "赤",
    "ブルー", "青", "グリーン", "グレー", "ピンク", "パープル", "オレンジ",
    "black", "white", "silver", "gold", "red", "blue", "green", "grey", "gray",
    "bk", "wh",
    # Condition / state
    "新品", "中古", "未使用", "美品", "良品", "並品", "動作確認済み", "動作保証",
    "動作品", "完動品", "現状品", "訳あり", "難あり",
    # Generic packaging / quantity
    "セット", "まとめ", "本体", "限定", "付属品", "箱付き", "箱なし",
    "1個", "2個", "3個",
    # Size / generic specs
    "型", "インチ", "サイズ",
})


@dataclass
class CandidateProposal:
    keyword: str
    strategy: str
    confidence: float
    parent_keyword_id: int | None
    reasoning: str


def generate_all(
    insights: KeywordInsights,
    db: Session,
    max_per_strategy: int = 10,
    demand_products: list[dict] | None = None,
) -> list[CandidateProposal]:
    """Run all generation strategies and return deduplicated candidates."""
    existing = _get_existing_keywords(db)

    candidates: list[CandidateProposal] = []
    candidates.extend(generate_brand_expansion(insights, existing, max_per_strategy))
    candidates.extend(generate_title_decomp(insights, existing, max_per_strategy))
    candidates.extend(generate_category_keywords(insights, existing, max_per_strategy))
    candidates.extend(generate_synonyms(insights, existing, max_per_strategy))
    candidates.extend(generate_series_expansion(db, existing, max_per_strategy))
    candidates.extend(generate_demand(demand_products or [], existing, max_per_strategy))

    # Final dedup + apparel filter
    seen = set()
    unique = []
    for c in candidates:
        key = c.keyword.lower().strip()
        if key not in seen and key not in existing and not is_apparel(c.keyword):
            seen.add(key)
            unique.append(c)

    logger.info(
        "Generated %d unique candidates from %d total",
        len(unique), len(candidates),
    )
    return unique


def generate_brand_expansion(
    insights: KeywordInsights,
    existing: set[str],
    max_count: int = 10,
) -> list[CandidateProposal]:
    """Strategy 1: Combine high-performing brands with product type tokens."""
    candidates = []

    # Get top brands (avg_profit > 3000, 3+ deals, total profit > 15000)
    good_brands = [
        b for b in insights.brand_patterns
        if b.avg_profit >= 3000 and b.deal_count >= 3 and b.total_profit >= 15000
    ]

    # Get top product type tokens, excluding low-quality tokens
    top_types = [
        p.product_type for p in insights.product_type_patterns[:15]
        if p.product_type not in _LOW_QUALITY_TOKENS
        and p.product_type.lower() not in _LOW_QUALITY_TOKENS
    ]

    # Find parent keyword ID for lineage
    brand_parent_map: dict[str, int] = {}
    for kp in insights.top_keywords:
        for brand in good_brands:
            if brand.brand_name in kp.keyword.lower():
                brand_parent_map[brand.brand_name] = kp.keyword_id
                break

    for brand in good_brands:
        parent_id = brand_parent_map.get(brand.brand_name)

        for ptype in top_types:
            # Skip if the product type is the brand itself
            if ptype.lower() == brand.brand_name.lower():
                continue

            keyword = f"{brand.brand_name} {ptype}"
            if keyword.lower() in existing:
                continue

            candidates.append(CandidateProposal(
                keyword=keyword,
                strategy="brand",
                confidence=0.7,
                parent_keyword_id=parent_id,
                reasoning=f"ブランド「{brand.brand_name}」({brand.deal_count}件Deal, 平均利益¥{brand.avg_profit:,.0f}) × 商品種別「{ptype}」",
            ))

            if len(candidates) >= max_count:
                return candidates

    return candidates


def generate_title_decomp(
    insights: KeywordInsights,
    existing: set[str],
    max_count: int = 10,
) -> list[CandidateProposal]:
    """Strategy 2: Recombine high-scoring title tokens into new keywords."""
    candidates = []

    # Get top tokens (score > 1.0), filtering low-quality and generic tokens
    top_tokens = [
        (token, score) for token, score in insights.title_tokens.items()
        if score >= 1.0
        and token.lower() not in KNOWN_BRANDS
        and token not in _LOW_QUALITY_TOKENS
        and token.lower() not in _LOW_QUALITY_TOKENS
        and len(token) >= 3
    ][:20]

    if len(top_tokens) < 2:
        return candidates

    # Find a parent keyword for lineage
    parent_id = insights.top_keywords[0].keyword_id if insights.top_keywords else None

    # Generate 2-word combinations
    for i, (t1, s1) in enumerate(top_tokens):
        for t2, s2 in top_tokens[i + 1:]:
            keyword = f"{t1} {t2}"
            if keyword.lower() in existing:
                continue

            combined_score = (s1 + s2) / 2
            candidates.append(CandidateProposal(
                keyword=keyword,
                strategy="title",
                confidence=0.6,
                parent_keyword_id=parent_id,
                reasoning=f"高スコアトークン「{t1}」(スコア{s1:.1f}) + 「{t2}」(スコア{s2:.1f})",
            ))

            if len(candidates) >= max_count:
                return candidates

    return candidates


def generate_category_keywords(
    insights: KeywordInsights,
    existing: set[str],
    max_count: int = 10,
) -> list[CandidateProposal]:
    """Strategy 3: Use brand + condition variants from high-performing brands."""
    candidates = []

    good_brands = [
        b for b in insights.brand_patterns
        if b.avg_profit >= 3000 and b.deal_count >= 3 and b.total_profit >= 15000
    ]

    brand_parent_map: dict[str, int] = {}
    for kp in insights.top_keywords:
        for brand in good_brands:
            if brand.brand_name in kp.keyword.lower():
                brand_parent_map[brand.brand_name] = kp.keyword_id
                break

    for brand in good_brands:
        parent_id = brand_parent_map.get(brand.brand_name)
        for suffix in CONDITION_SUFFIXES:
            keyword = f"{brand.brand_name} {suffix}"
            if keyword.lower() in existing:
                continue
            candidates.append(CandidateProposal(
                keyword=keyword,
                strategy="category",
                confidence=0.65,
                parent_keyword_id=parent_id,
                reasoning=f"高利益ブランド「{brand.brand_name}」のバリエーション「{suffix}」",
            ))
            if len(candidates) >= max_count:
                return candidates

    return candidates


def generate_synonyms(
    insights: KeywordInsights,
    existing: set[str],
    max_count: int = 10,
) -> list[CandidateProposal]:
    """Strategy 4: English↔Katakana, abbreviation expansion, condition variants."""
    candidates = []

    # Build reverse synonym map
    reverse_map = {v.lower(): k for k, v in SYNONYM_MAP.items()}
    reverse_map.update({k: v for k, v in SYNONYM_MAP.items()})

    for kp in insights.top_keywords:
        if kp.performance_score < 0.1:
            continue

        kw_lower = kp.keyword.lower()
        tokens = kw_lower.split()

        # Token-level synonym replacement
        for i, token in enumerate(tokens):
            if token in reverse_map:
                new_tokens = tokens.copy()
                new_tokens[i] = reverse_map[token]
                keyword = " ".join(new_tokens)
                if keyword.lower() not in existing:
                    candidates.append(CandidateProposal(
                        keyword=keyword,
                        strategy="synonym",
                        confidence=0.5,
                        parent_keyword_id=kp.keyword_id,
                        reasoning=f"「{kp.keyword}」の類義語: {token} → {reverse_map[token]}",
                    ))

            # Abbreviation expansion
            if token in ABBREVIATION_MAP:
                expanded = ABBREVIATION_MAP[token]
                keyword = kp.keyword.replace(token, expanded)
                if keyword.lower() not in existing:
                    candidates.append(CandidateProposal(
                        keyword=keyword,
                        strategy="synonym",
                        confidence=0.5,
                        parent_keyword_id=kp.keyword_id,
                        reasoning=f"「{kp.keyword}」の略称展開: {token} → {expanded}",
                    ))

        if len(candidates) >= max_count:
            break

    return candidates[:max_count]


# ---------------------------------------------------------------------------
# Brand name forms for Yahoo Auction search
# canonical → (Japanese, English)
# ---------------------------------------------------------------------------
_BRAND_BOTH_FORMS: dict[str, tuple[str, str]] = {
    "nintendo": ("任天堂", "Nintendo"),
    "sony": ("ソニー", "SONY"),
    "playstation": ("プレイステーション", "PlayStation"),
    "microsoft": ("マイクロソフト", "Microsoft"),
    "sega": ("セガ", "SEGA"),
    "bandai": ("バンダイ", "BANDAI"),
    "konami": ("コナミ", "KONAMI"),
    "capcom": ("カプコン", "CAPCOM"),
    "apple": ("Apple", "Apple"),
    "samsung": ("サムスン", "Samsung"),
    "panasonic": ("パナソニック", "Panasonic"),
    "sharp": ("シャープ", "SHARP"),
    "toshiba": ("東芝", "TOSHIBA"),
    "hitachi": ("日立", "HITACHI"),
    "canon": ("キヤノン", "Canon"),
    "nikon": ("ニコン", "Nikon"),
    "olympus": ("オリンパス", "OLYMPUS"),
    "fujifilm": ("富士フイルム", "FUJIFILM"),
    "casio": ("カシオ", "CASIO"),
    "epson": ("エプソン", "EPSON"),
    "bose": ("Bose", "Bose"),
    "jbl": ("JBL", "JBL"),
    "sennheiser": ("ゼンハイザー", "Sennheiser"),
    "audio-technica": ("オーディオテクニカ", "audio-technica"),
    "dyson": ("ダイソン", "Dyson"),
    "irobot": ("アイロボット", "iRobot"),
    "braun": ("ブラウン", "BRAUN"),
    "philips": ("フィリップス", "Philips"),
    "daikin": ("ダイキン", "DAIKIN"),
    "makita": ("マキタ", "Makita"),
    "mitsubishi": ("三菱", "三菱"),
    "buffalo": ("バッファロー", "BUFFALO"),
    "logicool": ("ロジクール", "Logicool"),
    "anker": ("Anker", "Anker"),
    "pioneer": ("パイオニア", "Pioneer"),
    "tiger": ("タイガー", "TIGER"),
    "zojirushi": ("象印", "象印"),
    "tefal": ("ティファール", "T-fal"),
    "delonghi": ("デロンギ", "DeLonghi"),
    "iris ohyama": ("アイリスオーヤマ", "IRIS OHYAMA"),
    "balmuda": ("バルミューダ", "BALMUDA"),
    "roomba": ("ルンバ", "Roomba"),
    "shure": ("Shure", "Shure"),
    "gopro": ("GoPro", "GoPro"),
    "lego": ("レゴ", "LEGO"),
    "twinbird": ("ツインバード", "TWINBIRD"),
    "elecom": ("エレコム", "ELECOM"),
}

# Cache: canonical brand → preferred Yahoo search form (populated by resolve_brand_preference)
brand_preference_cache: dict[str, str] = {}


def format_model_keyword(brand: str | None, model: str) -> str:
    """Format keyword for Yahoo Auction search.

    - Model 4+ chars → model only (型番だけで商品特定可能)
    - Model ≤3 chars → preferred brand name + model
    """
    if not brand:
        return model
    if len(model) >= 4:
        return model

    # Look up preferred form (dynamic cache > static Japanese default)
    preferred = brand_preference_cache.get(brand)
    if not preferred:
        forms = _BRAND_BOTH_FORMS.get(brand)
        if forms:
            preferred = forms[0]  # Default: Japanese form
        else:
            preferred = brand
    return f"{preferred} {model}"


async def resolve_brand_preference(scraper, brand: str) -> str:
    """Check Yahoo search counts for Japanese vs English brand name.

    Results are cached module-wide for reuse across scan cycles.
    """
    if brand in brand_preference_cache:
        return brand_preference_cache[brand]

    forms = _BRAND_BOTH_FORMS.get(brand)
    if not forms:
        brand_preference_cache[brand] = brand
        return brand

    ja_name, en_name = forms
    if ja_name == en_name:
        brand_preference_cache[brand] = ja_name
        return ja_name

    try:
        ja_results = await scraper.search(ja_name, page=1)
        en_results = await scraper.search(en_name, page=1)
        ja_count = len(ja_results) if ja_results else 0
        en_count = len(en_results) if en_results else 0
        winner = ja_name if ja_count >= en_count else en_name
        logger.info(
            "Brand preference resolved: %s → %s (JA '%s': %d, EN '%s': %d)",
            brand, winner, ja_name, ja_count, en_name, en_count,
        )
    except Exception:
        winner = ja_name  # Default to Japanese on error

    brand_preference_cache[brand] = winner
    return winner


_SERIES_DECOMPOSE_RE = re.compile(r"^([a-z]+)(\d+)([a-z]*)$")


def _decompose_model(model: str) -> tuple[str, int, str] | None:
    """Decompose model number into prefix + number + suffix.

    "xd900"    → ("xd", 900, "")
    "cfi1200a" → ("cfi", 1200, "a")
    "wh1000xm4" → None (complex model, skip)
    """
    m = _SERIES_DECOMPOSE_RE.match(model)
    if not m:
        return None
    return m.group(1), int(m.group(2)), m.group(3)


def _guess_step(num: int) -> int:
    """Guess the numeric step between sibling models."""
    if num >= 100 and num % 100 == 0:
        return 100   # xd900 → xd800, xd1000
    if num >= 10 and num % 10 == 0:
        return 10    # wf110 → wf100, wf120
    return 1         # ps5 → ps4, ps6


def generate_series_expansion(
    db: Session,
    existing: set[str],
    max_count: int = 10,
) -> list[CandidateProposal]:
    """Strategy 5: Generate sibling model numbers from profitable deals."""
    profitable_alerts = (
        db.query(DealAlert)
        .filter(
            DealAlert.gross_profit >= settings.series_expansion_min_profit,
            DealAlert.status != "rejected",
        )
        .order_by(DealAlert.gross_profit.desc())
        .limit(50)
        .all()
    )

    seen_models: set[str] = set()
    candidates: list[CandidateProposal] = []

    for alert in profitable_alerts:
        brand, models, _ = extract_product_info(alert.yahoo_title)
        for model in models:
            if model in seen_models:
                continue
            seen_models.add(model)

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
                keyword = format_model_keyword(brand, sibling_model)

                if keyword.lower() in existing:
                    continue

                candidates.append(CandidateProposal(
                    keyword=keyword,
                    strategy="series",
                    confidence=0.75,
                    parent_keyword_id=alert.keyword_id,
                    reasoning=f"利益確認済み「{brand or ''} {model}」(¥{alert.gross_profit:,})のシリーズ展開",
                ))

            if len(candidates) >= max_count:
                break
        if len(candidates) >= max_count:
            break

    return candidates[:max_count]


# Keepa brand → Yahoo-searchable short form
_BRAND_SHORT_MAP: dict[str, str] = {
    "ソニー・インタラクティブエンタテインメント": "ソニー",
    "sony interactive entertainment": "ソニー",
    "sony interactive entertainment inc.": "ソニー",
    "sony corporation": "ソニー",
    "sony group corporation": "ソニー",
    "パナソニック株式会社": "パナソニック",
    "panasonic corporation": "パナソニック",
    "panasonic holdings": "パナソニック",
    "任天堂株式会社": "任天堂",
    "nintendo co., ltd.": "任天堂",
    "シャープ株式会社": "シャープ",
    "sharp corporation": "シャープ",
    "日立グローバルライフソリューションズ": "日立",
    "日立製作所": "日立",
    "ダイソン・テクノロジー": "ダイソン",
    "dyson technology limited": "ダイソン",
    "dyson": "ダイソン",
    "バッファロー": "バッファロー",
    "buffalo inc.": "バッファロー",
    "アイリスオーヤマ株式会社": "アイリスオーヤマ",
    "ブラザー工業株式会社": "ブラザー",
    "brother industries": "ブラザー",
    "キヤノン株式会社": "キヤノン",
    "canon inc.": "キヤノン",
    "エプソン販売株式会社": "エプソン",
    "seiko epson": "エプソン",
}

_BARCODE_RE = re.compile(r"^\d{8,}$")


def _clean_brand(brand: str) -> str:
    """Shorten/canonicalize Keepa brand names for Yahoo searchability."""
    if not brand:
        return ""
    stripped = brand.strip()
    lower = stripped.lower()
    for long_form, short_form in _BRAND_SHORT_MAP.items():
        if long_form.lower() in lower:
            return short_form
    if len(stripped) > 20:
        first_word = stripped.split()[0] if stripped.split() else stripped
        return first_word if len(first_word) >= 2 else ""
    return stripped


def _is_barcode(text: str) -> bool:
    """Detect barcodes/EAN codes masquerading as model numbers."""
    return bool(_BARCODE_RE.match(text.strip()))


def generate_demand(
    demand_products: list[dict],
    existing: set[str],
    max_count: int = 10,
) -> list[CandidateProposal]:
    """Strategy 6: Amazon中古で売れている商品の型番をキーワード候補に。

    Keepa Product Finderで取得した商品から型番を抽出し、
    ブランド+型番のキーワード候補を生成する。
    """
    candidates = []

    for p in demand_products:
        model = (p.get("model") or "").strip()
        brand = (p.get("brand") or "").strip()
        title = p.get("title") or ""

        # Filter: reject barcode/EAN in model field
        if model and _is_barcode(model):
            model = ""

        if not model or model == "None":
            # modelフィールドがない場合、タイトルから型番抽出を試みる
            models = extract_model_numbers_from_text(title)
            if not models:
                continue
            model = sorted(models)[0]

        # Reject barcode extracted from title too
        if _is_barcode(model):
            continue

        # Clean brand name for Yahoo searchability
        brand = _clean_brand(brand)

        # 型番フォーマット: 長い型番はブランド不要、短い型番はブランド付き
        keyword = format_model_keyword(brand, model) if brand else model

        # Skip keywords that are too short to be useful
        if len(keyword) < 4:
            continue

        if keyword.lower() in existing:
            continue

        stats = p.get("stats", {})
        drops30 = stats.get("salesRankDrops30", 0) if isinstance(stats, dict) else 0

        candidates.append(CandidateProposal(
            keyword=keyword,
            strategy="demand",
            confidence=0.80,
            parent_keyword_id=None,
            reasoning=f"Amazon中古で月{drops30}回売れている商品",
        ))

        if len(candidates) >= max_count:
            break

    return candidates


def _get_existing_keywords(db: Session) -> set[str]:
    """Get all existing keyword strings (WatchedKeyword + pending candidates)."""
    keywords = set()

    for kw in db.query(WatchedKeyword.keyword).all():
        keywords.add(kw[0].lower().strip())

    for kc in (
        db.query(KeywordCandidate.keyword)
        .filter(KeywordCandidate.status.notin_(["rejected"]))
        .all()
    ):
        keywords.add(kc[0].lower().strip())

    return keywords
