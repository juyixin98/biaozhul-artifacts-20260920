"""Maintenance endpoints, supervisor only (e.g. deterministic seat expiry)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_db, require_supervisor
from app.models import User
from app.services import enrollment_service

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post("/expire-seats")
def expire_seats(
    program_id: int | None = None,
    session: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> dict:
    """Process 48h seat holds that are due and promote the waitlist.

    Uses the application clock, so deployment/tests can control "now".
    """
    return enrollment_service.sweep_expired(session, program_id)
