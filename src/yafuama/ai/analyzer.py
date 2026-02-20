"""Deal history pattern analyzer for AI keyword discovery.

Mines DealAlert records to extract profitable brands, product types,
price ranges, and keyword performance scores.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..models import DealAlert, WatchedKeyword

# Known brands common in Yahoo→Amazon reselling
KNOWN_BRANDS = {
    # Gaming
    "nintendo", "sony", "playstation", "xbox", "sega", "bandai", "konami",
    "capcom", "square", "enix", "namco", "taito",
    # Electronics
    "apple", "samsung", "panasonic", "sharp", "toshiba", "hitachi",
    "canon", "nikon", "olympus", "fujifilm", "casio", "epson",
    "bose", "jbl", "sennheiser", "audio-technica",
    # Home / Lifestyle
    "dyson", "iRobot", "roomba", "daikin", "braun", "philips",
    # Toys / Collectibles
    "pokemon", "lego", "takara", "tomy", "medicom", "figma",
    "goodsmile", "kotobukiya", "megahouse", "bearbrick",
}

# Japanese stopwords and listing noise to filter out
STOPWORDS = {
    "送料", "無料", "中古", "美品", "新品", "未使用", "未開封", "即決",
    "セット", "まとめ", "ジャンク", "動作", "確認", "済み", "品", "付き",
    "箱", "あり", "なし", "本体", "のみ", "限定", "非売品",
    "の", "が", "で", "に", "は", "を", "と", "も", "や", "から", "まで",
    "より", "こそ", "さえ", "でも", "しか", "など", "ほど",
    "a", "the", "and", "or", "for", "with", "in", "on", "at", "to", "of",
    "is", "it", "no", "not", "be", "an", "as", "by",
}

# Minimum characters for a meaningful token
MIN_TOKEN_LEN = 2


@dataclass
class BrandPattern:
    brand_name: str
    deal_count: int
    avg_profit: float
    total_profit: int
    example_keywords: list[str] = field(default_factory=list)


@dataclass
class ProductTypePattern:
    product_type: str
    deal_count: int
    avg_profit: float
    score: float = 0.0  # frequency * avg_profit weight


@dataclass
class PriceRangePattern:
    range_label: str
    min_price: int
    max_price: int
    deal_count: int
    avg_margin: float


@dataclass
class KeywordPerformance:
    keyword_id: int
    keyword: str
    total_deals: int
    total_scans: int
    avg_gross_profit: float
    avg_gross_margin: float
    performance_score: float
    source: str


@dataclass
class KeywordInsights:
    """Complete analysis results from deal history mining."""
    top_keywords: list[KeywordPerformance]
    brand_patterns: list[BrandPattern]
    product_type_patterns: list[ProductTypePattern]
    price_range_patterns: list[PriceRangePattern]
    title_tokens: dict[str, float]  # token -> score
    total_deals: int
    total_keywords: int


def analyze_deal_history(db: Session) -> KeywordInsights:
    """Main entry point: analyze all DealAlert records for patterns."""
    alerts = db.query(DealAlert).all()
    keywords = db.query(WatchedKeyword).all()

    kw_map = {kw.id: kw for kw in keywords}

    # Group alerts by keyword
    alerts_by_kw: dict[int, list[DealAlert]] = defaultdict(list)
    for a in alerts:
        alerts_by_kw[a.keyword_id].append(a)

    # Keyword performance
    kw_performances = []
    for kw in keywords:
        kw_alerts = alerts_by_kw.get(kw.id, [])
        score = compute_performance_score(kw, kw_alerts)
        kw.performance_score = score  # Update in-place for DB persistence

        if kw_alerts:
            avg_profit = sum(a.gross_profit for a in kw_alerts) / len(kw_alerts)
            avg_margin = sum(a.gross_margin_pct for a in kw_alerts) / len(kw_alerts)
        else:
            avg_profit = avg_margin = 0.0

        kw_performances.append(KeywordPerformance(
            keyword_id=kw.id,
            keyword=kw.keyword,
            total_deals=kw.total_deals_found,
            total_scans=kw.total_scans,
            avg_gross_profit=avg_profit,
            avg_gross_margin=avg_margin,
            performance_score=score,
            source=kw.source,
        ))

    kw_performances.sort(key=lambda k: k.performance_score, reverse=True)

    # Extract patterns from all alerts
    brands = extract_brand_patterns(alerts, kw_map)
    products = extract_product_types(alerts)
    price_ranges = extract_price_ranges(alerts)
    tokens = extract_title_tokens(alerts)

    return KeywordInsights(
        top_keywords=kw_performances,
        brand_patterns=brands,
        product_type_patterns=products,
        price_range_patterns=price_ranges,
        title_tokens=tokens,
        total_deals=len(alerts),
        total_keywords=len(keywords),
    )


def compute_performance_score(kw: WatchedKeyword, alerts: list[DealAlert]) -> float:
    """Calculate a 0.0-1.0 performance score for a keyword."""
    scans = kw.total_scans
    if scans == 0:
        return 0.0

    deals = kw.total_deals_found
    gross = kw.total_gross_profit

    # Deal hit rate
    deal_rate = min(deals / max(scans, 1), 1.0)

    # Average profit per deal
    avg_profit = gross / max(deals, 1)
    profit_score = min(avg_profit / 10000, 1.0)

    # Average margin from actual alerts
    if alerts:
        avg_margin = sum(a.gross_margin_pct for a in alerts) / len(alerts)
        margin_score = min(avg_margin / 100, 1.0)
    else:
        margin_score = 0.0

    # Recency bonus
    recency = 0.0
    if alerts:
        most_recent = max(a.notified_at for a in alerts)
        # Handle naive datetimes from SQLite
        now = datetime.now(timezone.utc)
        if most_recent.tzinfo is None:
            most_recent = most_recent.replace(tzinfo=timezone.utc)
        days_ago = (now - most_recent).days
        if days_ago <= 7:
            recency = 1.0
        elif days_ago <= 14:
            recency = 0.5

    return round(
        0.4 * profit_score +
        0.3 * deal_rate +
        0.2 * margin_score +
        0.1 * recency,
        4,
    )


def extract_brand_patterns(
    alerts: list[DealAlert],
    kw_map: dict[int, WatchedKeyword],
) -> list[BrandPattern]:
    """Extract brand names and their performance from deal titles."""
    brand_deals: dict[str, list[DealAlert]] = defaultdict(list)
    brand_keywords: dict[str, set[str]] = defaultdict(set)

    for alert in alerts:
        title_lower = alert.yahoo_title.lower()
        tokens = _tokenize(title_lower)

        for token in tokens:
            if token in KNOWN_BRANDS:
                brand_deals[token].append(alert)
                kw = kw_map.get(alert.keyword_id)
                if kw:
                    brand_keywords[token].add(kw.keyword)
                break  # One brand per title

    patterns = []
    for brand, deals in brand_deals.items():
        if len(deals) < 2:
            continue
        total_profit = sum(d.gross_profit for d in deals)
        patterns.append(BrandPattern(
            brand_name=brand,
            deal_count=len(deals),
            avg_profit=total_profit / len(deals),
            total_profit=total_profit,
            example_keywords=list(brand_keywords[brand])[:5],
        ))

    patterns.sort(key=lambda b: b.total_profit, reverse=True)
    return patterns


def extract_product_types(alerts: list[DealAlert]) -> list[ProductTypePattern]:
    """Extract meaningful product type tokens from deal titles."""
    token_deals: dict[str, list[DealAlert]] = defaultdict(list)

    for alert in alerts:
        tokens = _tokenize(alert.yahoo_title)
        seen = set()
        for t in tokens:
            if t in seen or t in STOPWORDS or t in KNOWN_BRANDS:
                continue
            if len(t) < MIN_TOKEN_LEN:
                continue
            seen.add(t)
            token_deals[t].append(alert)

    patterns = []
    for token, deals in token_deals.items():
        if len(deals) < 3:
            continue
        avg_profit = sum(d.gross_profit for d in deals) / len(deals)
        score = len(deals) * min(avg_profit / 5000, 2.0)
        patterns.append(ProductTypePattern(
            product_type=token,
            deal_count=len(deals),
            avg_profit=avg_profit,
            score=score,
        ))

    patterns.sort(key=lambda p: p.score, reverse=True)
    return patterns[:30]  # Top 30


def extract_price_ranges(alerts: list[DealAlert]) -> list[PriceRangePattern]:
    """Bucket deals by Yahoo price range."""
    buckets = [
        ("0-3000", 0, 3000),
        ("3000-5000", 3000, 5000),
        ("5000-10000", 5000, 10000),
        ("10000-30000", 10000, 30000),
        ("30000+", 30000, 999999999),
    ]

    patterns = []
    for label, lo, hi in buckets:
        deals = [a for a in alerts if lo <= a.yahoo_price < hi]
        if not deals:
            continue
        avg_margin = sum(d.gross_margin_pct for d in deals) / len(deals)
        patterns.append(PriceRangePattern(
            range_label=label,
            min_price=lo,
            max_price=hi,
            deal_count=len(deals),
            avg_margin=round(avg_margin, 1),
        ))

    return patterns


def extract_title_tokens(alerts: list[DealAlert]) -> dict[str, float]:
    """Build a token -> score map from deal titles.

    Score = frequency * avg_profit_weight. Higher means more interesting.
    """
    token_profits: dict[str, list[int]] = defaultdict(list)

    for alert in alerts:
        tokens = _tokenize(alert.yahoo_title)
        seen = set()
        for t in tokens:
            if t in seen or t in STOPWORDS or len(t) < MIN_TOKEN_LEN:
                continue
            seen.add(t)
            token_profits[t].append(alert.gross_profit)

    scores = {}
    for token, profits in token_profits.items():
        if len(profits) < 2:
            continue
        avg_p = sum(profits) / len(profits)
        scores[token] = round(len(profits) * min(avg_p / 5000, 2.0), 3)

    return dict(sorted(scores.items(), key=lambda x: x[1], reverse=True))


def _tokenize(text: str) -> list[str]:
    """Split text into meaningful tokens."""
    # Normalize: lowercase, replace common separators
    text = text.lower().strip()
    # Split on whitespace, punctuation, and Japanese particles
    tokens = re.split(r'[\s\-_/\\,;:!?。、（）\(\)\[\]【】「」『』]+', text)
    return [t for t in tokens if t]
