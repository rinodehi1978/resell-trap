"""AI Discovery API endpoints."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import DiscoveryLog, KeywordCandidate, WatchedKeyword
from ..matcher import is_apparel, keywords_are_similar
from ..schemas import (
    DiscoveryCycleResponse,
    DiscoveryInsightsResponse,
    DiscoveryLogResponse,
    DiscoveryStatusResponse,
    KeywordCandidateListResponse,
    KeywordCandidateResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/discovery", tags=["discovery"])


@router.get("/status", response_model=DiscoveryStatusResponse)
def discovery_status(db: Session = Depends(get_db)):
    """Get current AI discovery engine status and stats."""
    last_log = (
        db.query(DiscoveryLog)
        .order_by(DiscoveryLog.id.desc())
        .first()
    )

    total_ai = (
        db.query(WatchedKeyword)
        .filter(WatchedKeyword.source != "manual")
        .count()
    )
    active_ai = (
        db.query(WatchedKeyword)
        .filter(
            WatchedKeyword.source != "manual",
            WatchedKeyword.is_active == True,  # noqa: E712
        )
        .count()
    )
    pending = (
        db.query(KeywordCandidate)
        .filter(KeywordCandidate.status.in_(["pending", "validated"]))
        .count()
    )

    return DiscoveryStatusResponse(
        enabled=settings.discovery_enabled and settings.keepa_enabled,
        last_cycle=last_log,
        total_ai_keywords=total_ai,
        active_ai_keywords=active_ai,
        pending_candidates=pending,
    )


@router.post("/trigger", response_model=DiscoveryCycleResponse)
async def trigger_discovery():
    """Manually trigger a discovery cycle."""
    from ..main import app_state

    engine = app_state.get("discovery_engine")
    if engine is None:
        raise HTTPException(503, "Discovery engine is not available")

    result = await engine.run_discovery_cycle()
    return DiscoveryCycleResponse(
        candidates_generated=result.candidates_generated,
        candidates_validated=result.candidates_validated,
        keywords_added=result.keywords_added,
        keywords_deactivated=result.keywords_deactivated,
        keepa_tokens_used=result.keepa_tokens_used,
    )


@router.get("/candidates", response_model=KeywordCandidateListResponse)
def list_candidates(
    status: str | None = None,
    db: Session = Depends(get_db),
):
    """List keyword candidates (optionally filtered by status).

    Automatically filters out candidates that are similar to existing
    WatchedKeywords, so the user only sees genuinely new suggestions.
    """
    q = db.query(KeywordCandidate)
    if status:
        q = q.filter(KeywordCandidate.status == status)
    else:
        # Default: show actionable candidates
        q = q.filter(KeywordCandidate.status.in_(["pending", "validated"]))

    candidates = q.order_by(KeywordCandidate.confidence.desc()).limit(200).all()

    # Filter out candidates similar to existing WatchedKeywords
    existing_kws = [
        kw.keyword
        for kw in db.query(WatchedKeyword.keyword).all()
    ]
    filtered = []
    for c in candidates:
        # Skip apparel / fashion keywords
        if is_apparel(c.keyword):
            continue
        if any(keywords_are_similar(c.keyword, ek) for ek in existing_kws):
            continue
        # Also deduplicate within the candidate list itself (keep highest confidence first)
        if any(keywords_are_similar(c.keyword, kept.keyword) for kept in filtered):
            continue
        filtered.append(c)

    return KeywordCandidateListResponse(candidates=filtered[:100], total=len(filtered))


@router.post("/candidates/{candidate_id}/approve")
def approve_candidate(candidate_id: int, db: Session = Depends(get_db)):
    """Approve a candidate and create a WatchedKeyword."""
    kc = db.get(KeywordCandidate, candidate_id)
    if not kc:
        raise HTTPException(404, "Candidate not found")

    if kc.status not in ("pending", "validated"):
        raise HTTPException(400, f"Candidate status is '{kc.status}', cannot approve")

    # Block apparel keywords
    if is_apparel(kc.keyword):
        raise HTTPException(400, "アパレル関連キーワードは登録できません")

    # Check for duplicate keyword
    existing = db.query(WatchedKeyword).filter(WatchedKeyword.keyword == kc.keyword).first()
    if existing:
        raise HTTPException(409, f"Keyword '{kc.keyword}' already exists")

    kw = WatchedKeyword(
        keyword=kc.keyword,
        source=f"ai_{kc.strategy}",
        parent_keyword_id=kc.parent_keyword_id,
        confidence=kc.confidence,
        is_active=True,
    )
    db.add(kw)

    kc.status = "approved"
    kc.resolved_at = datetime.now(timezone.utc)

    # Auto-reject similar pending candidates to reduce clutter
    pending = (
        db.query(KeywordCandidate)
        .filter(
            KeywordCandidate.id != kc.id,
            KeywordCandidate.status.in_(["pending", "validated"]),
        )
        .all()
    )
    auto_rejected = 0
    now = datetime.now(timezone.utc)
    for pc in pending:
        if keywords_are_similar(pc.keyword, kc.keyword):
            pc.status = "rejected"
            pc.resolved_at = now
            auto_rejected += 1

    db.commit()
    logger.info(
        "Approved '%s', auto-rejected %d similar candidates",
        kc.keyword, auto_rejected,
    )

    return {"ok": True, "keyword_id": kw.id, "auto_rejected": auto_rejected}


@router.post("/candidates/{candidate_id}/reject")
def reject_candidate(candidate_id: int, db: Session = Depends(get_db)):
    """Reject a candidate."""
    kc = db.get(KeywordCandidate, candidate_id)
    if not kc:
        raise HTTPException(404, "Candidate not found")

    kc.status = "rejected"
    kc.resolved_at = datetime.now(timezone.utc)
    db.commit()

    return {"ok": True}


@router.get("/insights", response_model=DiscoveryInsightsResponse)
def discovery_insights(db: Session = Depends(get_db)):
    """View the latest analysis results (brands, product types, etc.)."""
    from ..ai.analyzer import analyze_deal_history

    insights = analyze_deal_history(db)

    return DiscoveryInsightsResponse(
        top_brands=[
            {"brand": b.brand_name, "deals": b.deal_count, "avg_profit": b.avg_profit}
            for b in insights.brand_patterns[:10]
        ],
        top_product_types=[
            {"type": p.product_type, "deals": p.deal_count, "score": p.score}
            for p in insights.product_type_patterns[:10]
        ],
        price_ranges=[
            {"range": r.range_label, "deals": r.deal_count, "avg_margin": r.avg_margin}
            for r in insights.price_range_patterns
        ],
        keyword_count=insights.total_keywords,
        deal_count=insights.total_deals,
    )


@router.get("/log", response_model=list[DiscoveryLogResponse])
def discovery_log(db: Session = Depends(get_db)):
    """Get discovery cycle history (most recent first)."""
    logs = (
        db.query(DiscoveryLog)
        .order_by(DiscoveryLog.id.desc())
        .limit(20)
        .all()
    )
    return logs


@router.post("/candidates/cleanup")
def cleanup_candidates(db: Session = Depends(get_db)):
    """Bulk-reject stale and low-quality pending candidates."""
    from ..ai.engine import DiscoveryEngine

    rejected = DiscoveryEngine._cleanup_stale_candidates(db)
    db.commit()

    remaining = (
        db.query(KeywordCandidate)
        .filter(KeywordCandidate.status == "pending")
        .count()
    )

    return {"ok": True, "rejected": rejected, "remaining_pending": remaining}


@router.post("/seed")
async def seed_keywords(db: Session = Depends(get_db)):
    """Generate initial seed keywords via Claude API (cold-start helper).

    Only works when ANTHROPIC_API_KEY is configured.
    Returns a list of suggestions for user to review before adding.
    """
    if not settings.anthropic_api_key:
        raise HTTPException(
            400,
            "ANTHROPIC_API_KEY が設定されていません。.env に追加してください。",
        )

    from ..ai.llm import get_seed_keywords

    suggestions = await get_seed_keywords(settings.anthropic_api_key, count=40)
    if not suggestions:
        raise HTTPException(502, "Claude APIからの応答を取得できませんでした")

    # Filter out keywords that already exist
    existing = {
        kw.keyword.lower()
        for kw in db.query(WatchedKeyword.keyword).all()
    }
    filtered = [
        s for s in suggestions
        if s["keyword"].lower() not in existing and not is_apparel(s["keyword"])
    ]

    return {"suggestions": filtered, "total": len(filtered), "filtered_out": len(suggestions) - len(filtered)}


@router.post("/seed/apply")
def apply_seed_keywords(
    body: dict,
    db: Session = Depends(get_db),
):
    """Bulk-add selected seed keywords as WatchedKeywords.

    Expects: {"keywords": [{"keyword": "...", "category": "...", "confidence": 0.7}, ...]}
    """
    kw_list = body.get("keywords", [])
    if not kw_list:
        raise HTTPException(400, "キーワードが指定されていません")

    added = 0
    for item in kw_list:
        keyword = str(item.get("keyword", "")).strip()
        if not keyword:
            continue

        # Skip duplicates and apparel
        if is_apparel(keyword):
            continue
        existing = db.query(WatchedKeyword).filter(WatchedKeyword.keyword == keyword).first()
        if existing:
            continue

        kw = WatchedKeyword(
            keyword=keyword,
            source="ai_seed",
            confidence=min(float(item.get("confidence", 0.5)), 1.0),
            is_active=True,
        )
        db.add(kw)
        added += 1

    db.commit()
    return {"ok": True, "added": added}
