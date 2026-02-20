"""Condition description template CRUD."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ConditionTemplate

router = APIRouter(prefix="/api/templates", tags=["templates"])


@router.get("")
def list_templates(db: Session = Depends(get_db)):
    """Return all condition templates keyed by condition_type."""
    templates = db.query(ConditionTemplate).all()
    return {
        t.condition_type: {
            "id": t.id,
            "condition_type": t.condition_type,
            "title": t.title,
            "body": t.body,
        }
        for t in templates
    }


@router.put("/{condition_type}")
def update_template(
    condition_type: str,
    body: dict,
    db: Session = Depends(get_db),
):
    """Update a condition template's body text."""
    t = db.query(ConditionTemplate).filter(
        ConditionTemplate.condition_type == condition_type
    ).first()
    if not t:
        raise HTTPException(404, f"Template '{condition_type}' not found")

    if "body" in body:
        t.body = body["body"]
    if "title" in body:
        t.title = body["title"]
    t.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True, "condition_type": condition_type}
