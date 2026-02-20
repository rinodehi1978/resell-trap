"""Rejection analysis engine.

Analyzes rejected deal alerts to:
1. Suggest specific rejection reasons before user chooses (suggest_reasons)
2. Extract patterns from confirmed rejections (analyze_single_rejection)
3. Aggregate batch statistics (analyze_all_rejections)
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..matcher import (
    _ACCESSORY_WORDS,
    _NOISE_WORDS,
    _extract_brand,
    _extract_model_numbers,
    match_products,
    normalize,
    tokenize,
)
from ..models import DealAlert, RejectionPattern

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# A. suggest_reasons — real-time title analysis for UI popup
# ---------------------------------------------------------------------------

def suggest_reasons(alert: DealAlert, db: Session) -> list[dict]:
    """Analyze yahoo/amazon titles and suggest specific rejection reasons.

    Returns a list of dicts: [{"reason": str, "label": str, "confidence": float}]
    sorted by confidence descending.
    """
    suggestions: list[dict] = []

    y_text = normalize(alert.yahoo_title or "")
    a_text = normalize(alert.amazon_title or "")
    y_tokens = tokenize(y_text)
    a_tokens = tokenize(a_text)

    # Run matcher to get diagnostic flags
    result = match_products(alert.yahoo_title or "", alert.amazon_title or "")

    # --- Model conflict ---
    y_models = _extract_model_numbers(y_tokens)
    a_models = _extract_model_numbers(a_tokens)
    if result.model_conflict and y_models and a_models:
        y_str = "/".join(sorted(y_models)[:3]).upper()
        a_str = "/".join(sorted(a_models)[:3]).upper()
        suggestions.append({
            "reason": "model_variant",
            "label": f"モデル番号違い: {y_str} ≠ {a_str}",
            "confidence": 0.95,
        })

    # --- Accessory conflict ---
    if result.accessory_conflict:
        # Find which accessory words were detected
        y_set = set(y_tokens)
        detected = y_set & _ACCESSORY_WORDS
        if detected:
            word = next(iter(detected))
            suggestions.append({
                "reason": "accessory",
                "label": f"部品/アクセサリー ({word})",
                "confidence": 0.90,
            })
        else:
            suggestions.append({
                "reason": "accessory",
                "label": "部品/アクセサリー (本体ではない可能性)",
                "confidence": 0.75,
            })

    # --- Multi-model (universal accessory) ---
    if not result.accessory_conflict and len(y_models) >= 2:
        models_str = "/".join(sorted(y_models)[:4]).upper()
        suggestions.append({
            "reason": "accessory",
            "label": f"汎用パーツ ({models_str}用)",
            "confidence": 0.80,
        })

    # --- Price ratio check ---
    if alert.yahoo_price > 0 and alert.sell_price > 0:
        ratio = alert.yahoo_price / alert.sell_price
        if ratio < 0.20:
            suggestions.append({
                "reason": "accessory",
                "label": f"価格差が大きい (仕入{alert.yahoo_price}円 vs 販売{alert.sell_price}円)",
                "confidence": 0.70,
            })
        elif ratio > 0.85:
            suggestions.append({
                "reason": "bad_price",
                "label": "利益が出ない価格帯",
                "confidence": 0.65,
            })

    # --- Brand conflict ---
    if result.brand_conflict:
        y_brand = _extract_brand(y_tokens)
        a_brand = _extract_brand(a_tokens)
        if y_brand and a_brand:
            suggestions.append({
                "reason": "wrong_product",
                "label": f"ブランド違い: {y_brand} ≠ {a_brand}",
                "confidence": 0.90,
            })

    # --- Type conflict ---
    if result.type_conflict:
        suggestions.append({
            "reason": "wrong_product",
            "label": "商品タイプ違い",
            "confidence": 0.70,
        })

    # --- Quantity conflict ---
    if result.qty_conflict:
        suggestions.append({
            "reason": "wrong_product",
            "label": "数量/セット内容違い",
            "confidence": 0.80,
        })

    # --- Yahoo-only tokens that signal partial item ---
    y_only = set(y_tokens) - set(a_tokens) - _NOISE_WORDS
    partial_signals = {"単体", "たんたい", "のみ", "only", "単品", "たんぴん", "ジャンク", "じゃんく"}
    found_partial = y_only & partial_signals
    if found_partial and not any(s["reason"] == "accessory" for s in suggestions):
        word = next(iter(found_partial))
        suggestions.append({
            "reason": "accessory",
            "label": f"部分品の可能性 ({word})",
            "confidence": 0.60,
        })

    # --- Check past rejection patterns for same ASIN ---
    past = db.query(RejectionPattern).filter(
        RejectionPattern.pattern_type == "problem_pair",
        RejectionPattern.pattern_key.contains(alert.amazon_asin),
        RejectionPattern.is_active == True,  # noqa: E712
    ).first()
    if past:
        data = _parse_json(past.pattern_data)
        prev_reason = data.get("reason", "wrong_product")
        suggestions.insert(0, {
            "reason": prev_reason,
            "label": f"過去に同様の却下あり ({past.hit_count}回)",
            "confidence": 0.98,
        })

    # Deduplicate by reason (keep highest confidence)
    seen: dict[str, dict] = {}
    for s in suggestions:
        key = s["reason"] + ":" + s["label"]
        if key not in seen or s["confidence"] > seen[key]["confidence"]:
            seen[key] = s
    result_list = sorted(seen.values(), key=lambda x: x["confidence"], reverse=True)

    return result_list[:5]


# ---------------------------------------------------------------------------
# B. analyze_single_rejection — extract and persist patterns from one rejection
# ---------------------------------------------------------------------------

def analyze_single_rejection(alert: DealAlert, reason: str, db: Session) -> None:
    """Extract patterns from a rejected alert and upsert to rejection_patterns."""
    y_text = normalize(alert.yahoo_title or "")
    a_text = normalize(alert.amazon_title or "")
    y_tokens = tokenize(y_text)
    a_tokens = tokenize(a_text)

    if reason == "accessory":
        _learn_accessory_words(alert, y_tokens, a_tokens, db)

    if reason == "model_variant":
        _learn_model_conflict(alert, y_tokens, a_tokens, db)

    if reason in ("wrong_product", "other"):
        _learn_problem_pair(alert, reason, db)

    if reason == "bad_price":
        _learn_price_pattern(alert, db)

    # Always record the problem pair for future reference
    _upsert_pattern(
        db,
        pattern_type="problem_pair",
        pattern_key=f"{alert.yahoo_auction_id}:{alert.amazon_asin}",
        pattern_data=json.dumps({
            "reason": reason,
            "yahoo_title": (alert.yahoo_title or "")[:100],
            "amazon_title": (alert.amazon_title or "")[:100],
            "yahoo_price": alert.yahoo_price,
            "sell_price": alert.sell_price,
        }),
        confidence=0.8,
    )

    # Check if this ASIN has been rejected 3+ times → block it
    asin_rejections = db.query(DealAlert).filter(
        DealAlert.amazon_asin == alert.amazon_asin,
        DealAlert.status == "rejected",
    ).count()
    if asin_rejections >= 3:
        _upsert_pattern(
            db,
            pattern_type="blocked_asin",
            pattern_key=alert.amazon_asin,
            pattern_data=json.dumps({
                "rejection_count": asin_rejections,
                "last_reason": reason,
            }),
            confidence=min(0.5 + asin_rejections * 0.1, 1.0),
        )

    logger.info(
        "Rejection patterns extracted: alert=%d reason=%s asin=%s",
        alert.id, reason, alert.amazon_asin,
    )


def _learn_accessory_words(
    alert: DealAlert,
    y_tokens: list[str],
    a_tokens: list[str],
    db: Session,
) -> None:
    """Find unknown accessory-indicating tokens from the Yahoo title."""
    y_set = set(y_tokens)
    a_set = set(a_tokens)
    # Tokens only in Yahoo side, not in Amazon, not noise, not already known
    y_only = y_set - a_set - _NOISE_WORDS - _ACCESSORY_WORDS
    # Filter to meaningful tokens (length >= 2, not pure digits)
    candidates = [t for t in y_only if len(t) >= 2 and not t.isdigit()]

    for word in candidates:
        _upsert_pattern(
            db,
            pattern_type="accessory_word",
            pattern_key=word,
            pattern_data=json.dumps({
                "word": word,
                "source_title": (alert.yahoo_title or "")[:100],
            }),
            confidence=0.3,  # Low initial confidence, grows with hits
        )


def _learn_model_conflict(
    alert: DealAlert,
    y_tokens: list[str],
    a_tokens: list[str],
    db: Session,
) -> None:
    """Record model number conflicts between Yahoo and Amazon titles."""
    y_models = _extract_model_numbers(y_tokens)
    a_models = _extract_model_numbers(a_tokens)
    if y_models and a_models and y_models != a_models:
        key = "|".join(sorted(y_models)) + ":" + "|".join(sorted(a_models))
        _upsert_pattern(
            db,
            pattern_type="model_conflict",
            pattern_key=key,
            pattern_data=json.dumps({
                "yahoo_models": sorted(y_models),
                "amazon_models": sorted(a_models),
                "yahoo_title": (alert.yahoo_title or "")[:100],
                "amazon_title": (alert.amazon_title or "")[:100],
            }),
            confidence=0.7,
        )


def _learn_problem_pair(alert: DealAlert, reason: str, db: Session) -> None:
    """Record a wrong_product/other pair for the specific ASIN."""
    # Already handled by the generic problem_pair in analyze_single_rejection
    pass


def _learn_price_pattern(alert: DealAlert, db: Session) -> None:
    """Record price anomaly patterns."""
    if alert.sell_price > 0:
        ratio = alert.yahoo_price / alert.sell_price
        _upsert_pattern(
            db,
            pattern_type="threshold_hint",
            pattern_key="price_ratio",
            pattern_data=json.dumps({
                "latest_ratio": round(ratio, 3),
                "yahoo_price": alert.yahoo_price,
                "sell_price": alert.sell_price,
            }),
            confidence=0.5,
        )


# ---------------------------------------------------------------------------
# C. analyze_all_rejections — batch analysis for discovery cycle
# ---------------------------------------------------------------------------

def analyze_all_rejections(db: Session) -> dict:
    """Aggregate analysis of all rejected alerts.

    Returns summary dict with stats and suggested improvements.
    """
    rejected = db.query(DealAlert).filter(DealAlert.status == "rejected").all()
    total_alerts = db.query(DealAlert).count()

    if not rejected:
        return {
            "total": 0,
            "by_reason": {},
            "false_positive_rate": 0.0,
            "new_accessory_words": [],
            "threshold_adjustment": 0.0,
        }

    # Count by reason
    by_reason = Counter(a.rejection_reason for a in rejected)

    # False positive rate
    fp_rate = len(rejected) / max(total_alerts, 1)

    # Aggregate accessory word candidates from patterns
    accessory_patterns = db.query(RejectionPattern).filter(
        RejectionPattern.pattern_type == "accessory_word",
        RejectionPattern.is_active == True,  # noqa: E712
        RejectionPattern.hit_count >= 2,
        RejectionPattern.confidence >= 0.6,
    ).all()
    new_accessory_words = [p.pattern_key for p in accessory_patterns]

    # Threshold adjustment: if >40% are false positives, suggest raising threshold
    threshold_adj = 0.0
    if fp_rate > 0.5 and len(rejected) >= 5:
        threshold_adj = 0.05
    elif fp_rate > 0.3 and len(rejected) >= 10:
        threshold_adj = 0.03

    if threshold_adj > 0:
        _upsert_pattern(
            db,
            pattern_type="threshold_hint",
            pattern_key="match_threshold",
            pattern_data=json.dumps({
                "adjustment": threshold_adj,
                "false_positive_rate": round(fp_rate, 3),
                "total_rejected": len(rejected),
                "total_alerts": total_alerts,
            }),
            confidence=min(0.5 + fp_rate, 1.0),
        )

    logger.info(
        "Batch rejection analysis: %d rejected / %d total (%.0f%% FP rate), "
        "%d learned accessory words, threshold adj=%.3f",
        len(rejected), total_alerts, fp_rate * 100,
        len(new_accessory_words), threshold_adj,
    )

    return {
        "total": len(rejected),
        "by_reason": dict(by_reason),
        "false_positive_rate": round(fp_rate, 3),
        "new_accessory_words": new_accessory_words,
        "threshold_adjustment": threshold_adj,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _upsert_pattern(
    db: Session,
    pattern_type: str,
    pattern_key: str,
    pattern_data: str,
    confidence: float,
) -> RejectionPattern:
    """Insert or update a rejection pattern."""
    existing = db.query(RejectionPattern).filter(
        RejectionPattern.pattern_type == pattern_type,
        RejectionPattern.pattern_key == pattern_key,
    ).first()

    if existing:
        existing.hit_count += 1
        existing.confidence = min(existing.confidence + 0.1, 1.0)
        existing.pattern_data = pattern_data
        existing.updated_at = datetime.now(timezone.utc)
        return existing
    else:
        p = RejectionPattern(
            pattern_type=pattern_type,
            pattern_key=pattern_key,
            pattern_data=pattern_data,
            hit_count=1,
            confidence=confidence,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(p)
        return p


def _parse_json(text: str) -> dict:
    """Safe JSON parse, returns {} on failure."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
