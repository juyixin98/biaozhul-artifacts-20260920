"""Care plan endpoints: create / revise / inspect / regenerate."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import Clock
from app.db import get_session
from app.deps import assert_unit_authorized, current_coordinator, get_clock, get_settings_dep
from app.models import CarePlan, Qualification, Task
from app.schemas import PlanCreateIn, PlanOut, PlanReviseIn
from app.config import Settings
from app.services.generation import GenerationError, create_plan, generate_tasks, revise_plan

router = APIRouter(prefix="/plans", tags=["plans"])


def _check_qualification_codes(db: Session, codes: list[str]) -> None:
    if not codes:
        return
    found = set(db.scalars(select(Qualification.code).where(Qualification.code.in_(codes))))
    missing = sorted(set(codes) - found)
    if missing:
        raise HTTPException(422, f"unknown qualification codes: {missing}")


def _to_out(plan: CarePlan, task_count: int | None = None) -> PlanOut:
    return PlanOut(
        id=plan.id,
        external_id=plan.external_id,
        revision=plan.revision,
        active=plan.active,
        client_name=plan.client_name,
        unit_id=plan.unit_id,
        timezone=plan.timezone,
        created_at=plan.created_at,
        generated_task_count=task_count,
    )


@router.post("", response_model=PlanOut, status_code=201)
def api_create_plan(
    payload: PlanCreateIn,
    db: Session = Depends(get_session),
    coordinator=Depends(current_coordinator),
    clock: Clock = Depends(get_clock),
) -> PlanOut:
    assert_unit_authorized(coordinator, payload.unit_id)
    for template in payload.templates:
        _check_qualification_codes(db, template.qualification_codes)
    try:
        plan = create_plan(
            db,
            external_id=payload.external_id,
            client_name=payload.client_name,
            unit_id=payload.unit_id,
            timezone=payload.timezone,
            templates=[t.model_dump() for t in payload.templates],
            now=clock.now(),
            actor_id=str(coordinator.id),
        )
    except GenerationError as exc:
        raise HTTPException(422, str(exc))
    count = len(list(db.scalars(select(Task).where(Task.plan_id == plan.id))))
    return _to_out(plan, count)


@router.post("/{external_id}/revisions", response_model=PlanOut, status_code=201)
def api_revise_plan(
    external_id: str,
    payload: PlanReviseIn,
    db: Session = Depends(get_session),
    coordinator=Depends(current_coordinator),
    clock: Clock = Depends(get_clock),
) -> PlanOut:
    current = db.scalar(
        select(CarePlan).where(CarePlan.external_id == external_id, CarePlan.active.is_(True))
    )
    if current is None:
        raise HTTPException(404, f"no active plan {external_id}")
    assert_unit_authorized(coordinator, current.unit_id)
    for template in payload.templates:
        _check_qualification_codes(db, template.qualification_codes)
    try:
        plan = revise_plan(
            db,
            external_id=external_id,
            client_name=payload.client_name,
            timezone=payload.timezone,
            templates=[t.model_dump() for t in payload.templates],
            now=clock.now(),
            actor_id=str(coordinator.id),
        )
    except GenerationError as exc:
        raise HTTPException(422, str(exc))
    count = len(list(db.scalars(select(Task).where(Task.plan_id == plan.id))))
    return _to_out(plan, count)


@router.get("", response_model=list[PlanOut])
def list_plans(db: Session = Depends(get_session)) -> list[PlanOut]:
    plans = list(db.scalars(select(CarePlan).order_by(CarePlan.id)))
    rows = db.execute(select(Task.plan_id)).all()
    from collections import Counter
    counts = Counter(r[0] for r in rows)
    return [_to_out(p, counts.get(p.id, 0)) for p in plans]


@router.get("/{external_id}", response_model=PlanOut)
def get_active_plan(external_id: str, db: Session = Depends(get_session)) -> PlanOut:
    plan = db.scalar(
        select(CarePlan).where(CarePlan.external_id == external_id, CarePlan.active.is_(True))
    )
    if plan is None:
        raise HTTPException(404, f"no active plan {external_id}")
    count = len(list(db.scalars(select(Task).where(Task.plan_id == plan.id))))
    return _to_out(plan, count)


@router.post("/{external_id}/generate", response_model=PlanOut)
def regenerate(
    external_id: str,
    db: Session = Depends(get_session),
    coordinator=Depends(current_coordinator),
    clock: Clock = Depends(get_clock),
    settings: Settings = Depends(get_settings_dep),
) -> PlanOut:
    """Idempotently (re)generate the rolling 14-day horizon — never duplicates."""
    plan = db.scalar(
        select(CarePlan).where(CarePlan.external_id == external_id, CarePlan.active.is_(True))
    )
    if plan is None:
        raise HTTPException(404, f"no active plan {external_id}")
    assert_unit_authorized(coordinator, plan.unit_id)
    generate_tasks(db, plan, now=clock.now(), horizon_days=settings.generation_horizon_days,
                   actor_id=str(coordinator.id))
    count = len(list(db.scalars(select(Task).where(Task.plan_id == plan.id))))
    return _to_out(plan, count)
