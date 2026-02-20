"""Dynamic matcher overrides loaded from rejection patterns.

Provides supplemental word lists that augment the static frozensets in matcher.py.
Loaded on startup and refreshed after each rejection.
"""

from __future__ import annotations

import json
import logging
import threading

logger = logging.getLogger(__name__)


class MatcherOverrides:
    """Thread-safe container for dynamic matcher data."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._extra_accessory_words: frozenset[str] = frozenset()
        self._blocked_pairs: set[tuple[str, str]] = set()
        self._blocked_asins: set[str] = set()
        self._threshold_adjustment: float = 0.0

    def reload(self) -> None:
        """Reload overrides from the rejection_patterns database table."""
        try:
            from .database import SessionLocal
            from .models import RejectionPattern
        except ImportError:
            logger.debug("Cannot import database modules, skipping reload")
            return

        db = SessionLocal()
        try:
            patterns = (
                db.query(RejectionPattern)
                .filter(RejectionPattern.is_active == True)  # noqa: E712
                .all()
            )

            accessory: set[str] = set()
            blocked_pairs: set[tuple[str, str]] = set()
            blocked_asins: set[str] = set()
            threshold_adj = 0.0

            for p in patterns:
                if p.pattern_type == "accessory_word" and p.hit_count >= 2 and p.confidence >= 0.6:
                    accessory.add(p.pattern_key)

                elif p.pattern_type == "problem_pair" and p.hit_count >= 2:
                    parts = p.pattern_key.split(":", 1)
                    if len(parts) == 2:
                        blocked_pairs.add((parts[0], parts[1]))

                elif p.pattern_type == "blocked_asin" and p.confidence >= 0.7:
                    blocked_asins.add(p.pattern_key)

                elif p.pattern_type == "threshold_hint" and p.pattern_key == "match_threshold":
                    data = _safe_json(p.pattern_data)
                    threshold_adj = data.get("adjustment", 0.0)

            with self._lock:
                self._extra_accessory_words = frozenset(accessory)
                self._blocked_pairs = blocked_pairs
                self._blocked_asins = blocked_asins
                self._threshold_adjustment = threshold_adj

            logger.info(
                "Matcher overrides reloaded: %d accessory words, %d blocked pairs, "
                "%d blocked ASINs, threshold adj=%.3f",
                len(accessory), len(blocked_pairs), len(blocked_asins), threshold_adj,
            )
        except Exception:
            logger.exception("Failed to reload matcher overrides")
        finally:
            db.close()

    @property
    def extra_accessory_words(self) -> frozenset[str]:
        with self._lock:
            return self._extra_accessory_words

    @property
    def blocked_pairs(self) -> set[tuple[str, str]]:
        with self._lock:
            return set(self._blocked_pairs)

    @property
    def blocked_asins(self) -> set[str]:
        with self._lock:
            return set(self._blocked_asins)

    @property
    def threshold_adjustment(self) -> float:
        with self._lock:
            return self._threshold_adjustment


def _safe_json(text: str) -> dict:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}


# Module-level singleton
overrides = MatcherOverrides()
