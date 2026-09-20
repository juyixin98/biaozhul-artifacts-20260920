"""Plan management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth import Actor, get_current_coordinator
from app.db import get_db
from app.deps import get_clock, serialize_plan
from app.models import CarePlan
from app.schemas import PlanCreate, PlanOut, PlanRevise
from app.services.errors import NotFoundError
from app.services.plans import create_plan, revise_plan

router = APIRouter(prefix="/api/plans", tags=["plans"])


@router.post("", response_model=PlanOut, status_code=201)
def create_care_plan(
    payload: PlanCreate,
    request: Request,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_coordinator),
) -> dict:
    actor.ensure_unit(payload.unit_id)
    plan = create_plan(
        db,
        get_clock(request),
        unit_id=payload.unit_id,
        title=payload.title,
        timezone=payload.timezone,
        period_days=payload.period_days,
        anchor_date=payload.anchor_date,
        slots=[s.model_dump() for s in payload.slots],
        qualification_ids=payload.qualification_ids,
        prerequisite_plan_ids=payload.prerequisite_plan_ids,
        coordinator_id=actor.id,
    )
    db.commit()
    return serialize_plan(db.get(CarePlan, plan.id))


@router.post("/{plan_id}/revise", response_model=PlanOut)
def revise_care_plan(
    plan_id: int,
    payload: PlanRevise,
    request: Request,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_coordinator),
) -> dict:
    actor.ensure_can_access_plan(db, plan_id)
    plan = revise_plan(
        db,
        get_clock(request),
        plan_id=plan_id,
        timezone=payload.timezone,
        period_days=payload.period_days,
        anchor_date=payload.anchor_date,
        slots=[s.model_dump() for s in payload.slots] if payload.slots else None,
        qualification_ids=payload.qualification_ids,
        prerequisite_plan_ids=payload.prerequisite_plan_ids,
        change_note=payload.change_note,
        coordinator_id=actor.id,
    )
    db.commit()
    return serialize_plan(db.get(CarePlan, plan.id))


@router.get("", response_model=list[PlanOut])
def list_plans(
    unit_id: int | None = None,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_coordinator),
) -> list[dict]:
    from sqlalchemy import select

    stmt = select(CarePlan).order_by(CarePlan.id)
    plans = [p for p in db.scalars(stmt) if p.unit_id in actor.unit_ids()]
    if unit_id is not None:
        actor.ensure_unit(unit_id)
        plans = [p for p in plans if p.unit_id == unit_id]
    return [serialize_plan(p) for p in plans]


@router.get("/{plan_id}", response_model=PlanOut)
def get_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_coordinator),
) -> dict:
    actor.ensure_can_access_plan(db, plan_id)
    plan = db.get(CarePlan, plan_id)
    if plan is None:
        raise NotFoundError(f"Plan {plan_id} not found")
    return serialize_plan(plan)
