"""Care plan endpoints and 14-day task generation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfoNotFoundError

from app.api.deps import require_coordinator
from app.api.serializers import plan_out
from app.database import get_db
from app.models import CarePlan
from app.schemas import GenerateOut, PlanIn, PlanOut, PlanUpdate
from app.services import plans as plans_service
from app.services.assignments import AuthorizationError

router = APIRouter(prefix="/plans", tags=["plans"])


def _get_plan_or_404(db: Session, plan_id: int) -> CarePlan:
    plan = db.get(CarePlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


def _authorize(db: Session, coordinator, unit_id: int) -> None:
    try:
        from app.services.assignments import authorize_unit
        authorize_unit(db, coordinator.id, unit_id)
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("", response_model=PlanOut, status_code=201)
def create_plan(
    payload: PlanIn,
    db: Session = Depends(get_db),
    coordinator=Depends(require_coordinator),
):
    _authorize(db, coordinator, payload.unit_id)
    try:
        plan = plans_service.create_plan(db, payload, actor=f"coordinator:{coordinator.id}")
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(status_code=422, detail=f"unknown timezone: {exc}") from exc
    except plans_service.PlanValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    prereqs = plans_service.prerequisite_plan_ids(db, plan.id)
    return plan_out(plan, prereqs)


@router.get("", response_model=list[PlanOut])
def list_plans(unit_id: int | None = None, db: Session = Depends(get_db)):
    stmt = select(CarePlan).order_by(CarePlan.id)
    if unit_id is not None:
        stmt = stmt.where(CarePlan.unit_id == unit_id)
    plans = list(db.scalars(stmt))
    return [plan_out(p, plans_service.prerequisite_plan_ids(db, p.id)) for p in plans]


@router.get("/{plan_id}", response_model=PlanOut)
def get_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = _get_plan_or_404(db, plan_id)
    return plan_out(plan, plans_service.prerequisite_plan_ids(db, plan_id))


@router.patch("/{plan_id}", response_model=PlanOut)
def update_plan(
    plan_id: int,
    payload: PlanUpdate,
    db: Session = Depends(get_db),
    coordinator=Depends(require_coordinator),
):
    plan = _get_plan_or_404(db, plan_id)
    _authorize(db, coordinator, plan.unit_id)
    try:
        plan, _stats = plans_service.update_plan(
            db, plan_id, payload, actor=f"coordinator:{coordinator.id}"
        )
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(status_code=422, detail=f"unknown timezone: {exc}") from exc
    except plans_service.PlanValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return plan_out(plan, plans_service.prerequisite_plan_ids(db, plan.id))


@router.post("/{plan_id}/generate", response_model=GenerateOut)
def generate(
    plan_id: int,
    db: Session = Depends(get_db),
    coordinator=Depends(require_coordinator),
):
    """Generate the next 14 days of tasks; repeat calls never duplicate work."""
    plan = _get_plan_or_404(db, plan_id)
    _authorize(db, coordinator, plan.unit_id)
    stats = plans_service.generate_tasks(
        db, plan, actor=f"coordinator:{coordinator.id}"
    )
    from app.api.serializers import task_out
    return GenerateOut(
        plan_id=stats.plan_id,
        horizon_days=stats.horizon_days,
        created=stats.created,
        updated=stats.updated,
        skipped_locked=stats.skipped_locked,
        cancelled_stale=stats.cancelled_stale,
        tasks=[task_out(t) for t in stats.tasks],
    )
