from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.access import can_view_user
from ..database import get_db
from ..detection.baseline import compute_baseline, latest_baseline
from ..models import Baseline, User, UserRole
from ..schemas import BaselineOut
from ..security import require_roles

router = APIRouter(prefix="/baselines", tags=["baselines"])

_VIEWERS = (UserRole.admin, UserRole.analyst, UserRole.manager)


class RecomputeRequest(BaseModel):
    user_ids: list[int] = Field(min_length=1, max_length=500)
    # Assessed local day; defaults to yesterday (latest complete day).
    target_day: date | None = None


class RecomputeResult(BaseModel):
    user_id: int
    baseline: BaselineOut
    alert_id: int | None = None


@router.post("/recompute", response_model=list[RecomputeResult])
def recompute(
    body: RecomputeRequest,
    db: Session = Depends(get_db),
    viewer: User = Depends(require_roles(UserRole.admin, UserRole.analyst)),
):
    """Append new baseline versions (never overwrite) and evaluate anomalies."""
    results = []
    for uid in body.user_ids:
        user = db.get(User, uid)
        if user is None or not can_view_user(db, viewer, user):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User {uid} not found or not in scope",
            )
        baseline, alert_id = compute_baseline(db, uid, body.target_day)
        results.append(RecomputeResult(user_id=uid, baseline=BaselineOut.model_validate(baseline), alert_id=alert_id))
    db.commit()
    return results


@router.get("/users/{user_id}/latest", response_model=BaselineOut)
def get_latest(
    user_id: int,
    db: Session = Depends(get_db),
    viewer: User = Depends(require_roles(*_VIEWERS)),
):
    user = db.get(User, user_id)
    if user is None or not can_view_user(db, viewer, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    baseline = latest_baseline(db, user_id)
    if baseline is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No baseline for user")
    return baseline


@router.get("/users/{user_id}/versions", response_model=list[BaselineOut])
def list_versions(
    user_id: int,
    db: Session = Depends(get_db),
    viewer: User = Depends(require_roles(*_VIEWERS)),
):
    user = db.get(User, user_id)
    if user is None or not can_view_user(db, viewer, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    rows = db.scalars(
        select(Baseline)
        .where(Baseline.user_id == user_id)
        .order_by(Baseline.version.desc())
    ).all()
    return list(rows)
