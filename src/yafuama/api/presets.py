"""ASIN-based listing preset CRUD."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ListingPreset

router = APIRouter(prefix="/api/presets", tags=["presets"])

MAX_PRESETS_PER_ASIN = 5


class PresetCreate(BaseModel):
    asin: str
    condition: str
    condition_note: str = ""
    shipping_pattern: str = "2_3_days"


@router.get("/{asin}")
def get_presets(asin: str, db: Session = Depends(get_db)):
    """Return presets for an ASIN (newest first, max 5)."""
    presets = (
        db.query(ListingPreset)
        .filter(ListingPreset.asin == asin)
        .order_by(ListingPreset.created_at.desc())
        .limit(MAX_PRESETS_PER_ASIN)
        .all()
    )
    return [
        {
            "id": p.id,
            "asin": p.asin,
            "condition": p.condition,
            "condition_note": p.condition_note,
            "shipping_pattern": p.shipping_pattern,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in presets
    ]


@router.post("", status_code=201)
def save_preset(body: PresetCreate, db: Session = Depends(get_db)):
    """Save a listing preset. Deduplicates and enforces max 5 per ASIN."""
    asin = body.asin.strip()
    if not asin:
        raise HTTPException(400, "ASIN must not be empty")

    # Deduplicate: same ASIN + condition + condition_note → update created_at
    existing = (
        db.query(ListingPreset)
        .filter(
            ListingPreset.asin == asin,
            ListingPreset.condition == body.condition,
            ListingPreset.condition_note == body.condition_note,
        )
        .first()
    )
    if existing:
        existing.shipping_pattern = body.shipping_pattern
        existing.created_at = datetime.now(timezone.utc)
        db.commit()
        return {"ok": True, "id": existing.id, "updated": True}

    # Create new preset
    preset = ListingPreset(
        asin=asin,
        condition=body.condition,
        condition_note=body.condition_note,
        shipping_pattern=body.shipping_pattern,
    )
    db.add(preset)
    db.flush()

    # Prune oldest if over limit
    all_presets = (
        db.query(ListingPreset)
        .filter(ListingPreset.asin == asin)
        .order_by(ListingPreset.created_at.desc())
        .all()
    )
    if len(all_presets) > MAX_PRESETS_PER_ASIN:
        for old in all_presets[MAX_PRESETS_PER_ASIN:]:
            db.delete(old)

    db.commit()
    return {"ok": True, "id": preset.id, "updated": False}
