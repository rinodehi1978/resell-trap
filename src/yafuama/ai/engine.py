"""Discovery engine: orchestrates the full AI keyword discovery cycle."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import settings
from ..database import SessionLocal
from ..matcher import keywords_are_similar
from ..models import DealAlert, DiscoveryLog, KeywordCandidate, WatchedKeyword
from .analyzer import analyze_deal_history, compute_performance_score
from .generator import generate_all
from .llm import get_llm_suggestions
from .validator import ValidationResult, should_auto_add, validate_candidate

logger = logging.getLogger(__name__)


@dataclass
class DiscoveryCycleResult:
    candidates_generated: int = 0
    candidates_validated: int = 0
    keywords_added: int = 0
    keywords_deactivated: int = 0
    keywords_deduped: int = 0
    keepa_tokens_used: int = 0
    errors: list[str] | None = None


class DiscoveryEngine:
    """Orchestrates the AI keyword discovery cycle."""

    def __init__(
        self,
        scraper,
        keepa_client,
        anthropic_api_key: str = "",
    ) -> None:
        self._scraper = scraper
        self._keepa = keepa_client
        self._anthropic_key = anthropic_api_key

    async def run_discovery_cycle(self) -> DiscoveryCycleResult:
        """Full cycle: Analyze → Generate → Validate → Register → Learn."""
        db = SessionLocal()
        log_entry = DiscoveryLog(started_at=datetime.now(timezone.utc))
        db.add(log_entry)
        db.flush()

        result = DiscoveryCycleResult()

        try:
            # 1. Analyze deal history
            insights = analyze_deal_history(db)
            logger.info(
                "Discovery: analyzed %d deals across %d keywords",
                insights.total_deals, insights.total_keywords,
            )

            # 1.5. Demand-based products from Keepa Product Finder
            demand_products: list[dict] = []
            if (
                self._keepa
                and settings.keepa_enabled
                and settings.demand_finder_enabled
            ):
                try:
                    demand_products = await self._keepa.product_finder(
                        selection={
                            "salesRankDrops30_gte": settings.demand_finder_min_drops30,
                            "current_USED_gte": settings.demand_finder_min_used_price,
                            "perPage": settings.demand_finder_max_results,
                        },
                    )
                    logger.info("Demand finder: %d products found", len(demand_products))
                except Exception as e:
                    logger.warning("Demand finder failed: %s", e)

            # 2. Generate candidates (skip if not enough data)
            if insights.total_deals >= settings.discovery_min_deals:
                candidates = generate_all(
                    insights, db, demand_products=demand_products,
                )

                # Optional: LLM suggestions
                if self._anthropic_key:
                    llm_candidates = await get_llm_suggestions(
                        insights, self._anthropic_key
                    )
                    candidates.extend(llm_candidates)

                # Save candidates to DB
                strategy_counts: dict[str, int] = {}
                for c in candidates:
                    kc = KeywordCandidate(
                        keyword=c.keyword,
                        strategy=c.strategy,
                        confidence=c.confidence,
                        parent_keyword_id=c.parent_keyword_id,
                        reasoning=c.reasoning,
                        status="pending",
                    )
                    db.add(kc)
                    strategy_counts[c.strategy] = strategy_counts.get(c.strategy, 0) + 1

                db.flush()
                result.candidates_generated = len(candidates)
                log_entry.strategy_breakdown = json.dumps(strategy_counts)

                # 3. Validate candidates (within token budget)
                token_budget = self._calculate_token_budget()
                pending = (
                    db.query(KeywordCandidate)
                    .filter(KeywordCandidate.status == "pending")
                    .order_by(KeywordCandidate.confidence.desc())
                    .all()
                )

                for kc in pending:
                    if token_budget <= 0:
                        break

                    proposal = _kc_to_proposal(kc)
                    vresult = await validate_candidate(
                        proposal, self._scraper, self._keepa, token_budget
                    )
                    token_budget -= vresult.keepa_tokens_used
                    result.keepa_tokens_used += vresult.keepa_tokens_used
                    result.candidates_validated += 1

                    kc.validation_result = vresult.to_json()
                    kc.resolved_at = datetime.now(timezone.utc)

                    if vresult.is_valid:
                        # 4. Register: auto-add or mark as validated
                        if should_auto_add(proposal, vresult, settings.discovery_auto_add_threshold):
                            self._register_keyword(kc, db)
                            kc.status = "auto_added"
                            result.keywords_added += 1
                        else:
                            kc.status = "validated"
                    else:
                        kc.status = "rejected"

            else:
                logger.info(
                    "Discovery: not enough deals (%d < %d), skipping history-based generation",
                    insights.total_deals, settings.discovery_min_deals,
                )
                # Still generate demand-based candidates even without deal history
                if demand_products:
                    from .generator import generate_demand, _get_existing_keywords
                    existing = _get_existing_keywords(db)
                    demand_candidates = generate_demand(demand_products, existing)
                    strategy_counts: dict[str, int] = {}
                    for c in demand_candidates:
                        kc = KeywordCandidate(
                            keyword=c.keyword,
                            strategy=c.strategy,
                            confidence=c.confidence,
                            parent_keyword_id=c.parent_keyword_id,
                            reasoning=c.reasoning,
                            status="pending",
                        )
                        db.add(kc)
                        strategy_counts[c.strategy] = strategy_counts.get(c.strategy, 0) + 1
                    db.flush()
                    result.candidates_generated = len(demand_candidates)
                    log_entry.strategy_breakdown = json.dumps(strategy_counts)

            # 5. Rejection learning: analyze all rejections and update matcher
            try:
                from .rejection_analyzer import analyze_all_rejections
                rejection_summary = analyze_all_rejections(db)
                if rejection_summary.get("new_accessory_words"):
                    logger.info(
                        "Rejection learning: %d new accessory words",
                        len(rejection_summary["new_accessory_words"]),
                    )
                from ..matcher_overrides import overrides
                overrides.reload()
            except Exception as e:
                logger.warning("Rejection analysis failed: %s", e)

            # 6. Learn: update all keyword performance scores
            result.keywords_deactivated = self._update_scores(db)

            # 7. Dedup: remove duplicate keywords
            result.keywords_deduped = self._cleanup_duplicate_keywords(db)

            # 8. Log
            log_entry.status = "completed"
            log_entry.finished_at = datetime.now(timezone.utc)
            log_entry.candidates_generated = result.candidates_generated
            log_entry.candidates_validated = result.candidates_validated
            log_entry.keywords_added = result.keywords_added
            log_entry.keywords_deactivated = result.keywords_deactivated
            log_entry.keepa_tokens_used = result.keepa_tokens_used

            db.commit()
            logger.info(
                "Discovery cycle complete: generated=%d, validated=%d, added=%d, deactivated=%d, deduped=%d",
                result.candidates_generated, result.candidates_validated,
                result.keywords_added, result.keywords_deactivated,
                result.keywords_deduped,
            )

        except Exception as e:
            logger.exception("Discovery cycle error: %s", e)
            log_entry.status = "error"
            log_entry.error_message = str(e)
            log_entry.finished_at = datetime.now(timezone.utc)
            db.commit()
            result.errors = [str(e)]
        finally:
            db.close()

        return result

    def _calculate_token_budget(self) -> int:
        """Calculate Keepa token budget for this discovery cycle."""
        tokens_left = self._keepa.tokens_left
        if tokens_left is None:
            return settings.discovery_token_budget

        # Use at most 10% of remaining tokens, capped by config
        return min(int(tokens_left * 0.1), settings.discovery_token_budget)

    def _register_keyword(self, kc: KeywordCandidate, db) -> None:
        """Create a WatchedKeyword from an approved candidate."""
        # Check AI keyword cap
        ai_count = (
            db.query(WatchedKeyword)
            .filter(
                WatchedKeyword.source != "manual",
                WatchedKeyword.is_active == True,  # noqa: E712
            )
            .count()
        )
        if ai_count >= settings.discovery_max_ai_keywords:
            logger.info("AI keyword cap reached (%d), skipping registration", ai_count)
            kc.status = "validated"  # Keep as validated for manual review
            return

        kw = WatchedKeyword(
            keyword=kc.keyword,
            source=f"ai_{kc.strategy}",
            parent_keyword_id=kc.parent_keyword_id,
            confidence=kc.confidence,
            is_active=True,
        )
        db.add(kw)
        logger.info("Auto-added AI keyword: %s (strategy=%s)", kc.keyword, kc.strategy)

    def _cleanup_duplicate_keywords(self, db) -> int:
        """Find and remove duplicate keywords, keeping the better performer.

        Rules:
        - manual vs AI duplicate → delete AI (respect user intent)
        - same source → keep the one with more deals/profit
        - tie → keep the older one (established longer)
        """
        keywords = (
            db.query(WatchedKeyword)
            .filter(WatchedKeyword.is_active == True)  # noqa: E712
            .order_by(WatchedKeyword.created_at)
            .all()
        )

        to_delete: set[int] = set()
        deleted = 0

        for i, kw_a in enumerate(keywords):
            if kw_a.id in to_delete:
                continue
            for kw_b in keywords[i + 1:]:
                if kw_b.id in to_delete:
                    continue
                if not keywords_are_similar(kw_a.keyword, kw_b.keyword):
                    continue

                # Decide which to remove
                loser = self._pick_loser(kw_a, kw_b, db)
                to_delete.add(loser.id)
                logger.info(
                    "Dedup: removing '%s' (id=%d, source=%s, deals=%d) "
                    "— duplicate of '%s' (id=%d, source=%s, deals=%d)",
                    loser.keyword, loser.id, loser.source, loser.total_deals_found,
                    (kw_a if loser.id != kw_a.id else kw_b).keyword,
                    (kw_a if loser.id != kw_a.id else kw_b).id,
                    (kw_a if loser.id != kw_a.id else kw_b).source,
                    (kw_a if loser.id != kw_a.id else kw_b).total_deals_found,
                )

        # Delete losers
        for kid in to_delete:
            kw = db.query(WatchedKeyword).filter(WatchedKeyword.id == kid).first()
            if kw:
                db.delete(kw)
                deleted += 1

        return deleted

    @staticmethod
    def _pick_loser(kw_a: WatchedKeyword, kw_b: WatchedKeyword, db) -> WatchedKeyword:
        """Between two duplicate keywords, pick the one to remove."""
        # Rule 1: manual always beats AI
        a_manual = kw_a.source == "manual"
        b_manual = kw_b.source == "manual"
        if a_manual and not b_manual:
            return kw_b
        if b_manual and not a_manual:
            return kw_a

        # Rule 2: more deals found wins
        if kw_a.total_deals_found != kw_b.total_deals_found:
            return kw_b if kw_a.total_deals_found > kw_b.total_deals_found else kw_a

        # Rule 3: higher total profit wins
        if kw_a.total_gross_profit != kw_b.total_gross_profit:
            return kw_b if kw_a.total_gross_profit > kw_b.total_gross_profit else kw_a

        # Rule 4: older keyword wins (established longer)
        return kw_b if kw_a.created_at <= kw_b.created_at else kw_a

    def _update_scores(self, db) -> int:
        """Update performance_score for all keywords and auto-deactivate underperformers."""
        keywords = db.query(WatchedKeyword).all()
        deactivated = 0

        for kw in keywords:
            # Recompute score from alerts
            alerts = kw.alerts
            kw.performance_score = compute_performance_score(kw, alerts)

            # Auto-deactivate underperforming AI keywords
            if (
                kw.source != "manual"
                and kw.is_active
                and kw.auto_deactivated_at is None
                and kw.total_scans >= settings.discovery_deactivation_scans
                and kw.performance_score < settings.discovery_deactivation_threshold
            ):
                kw.is_active = False
                kw.auto_deactivated_at = datetime.now(timezone.utc)
                deactivated += 1
                logger.info(
                    "Auto-deactivated AI keyword: %s (score=%.3f, scans=%d)",
                    kw.keyword, kw.performance_score, kw.total_scans,
                )

        return deactivated


def _kc_to_proposal(kc: KeywordCandidate):
    """Convert a KeywordCandidate DB record to a CandidateProposal."""
    from .generator import CandidateProposal

    return CandidateProposal(
        keyword=kc.keyword,
        strategy=kc.strategy,
        confidence=kc.confidence,
        parent_keyword_id=kc.parent_keyword_id,
        reasoning=kc.reasoning,
    )
