"""Audit history and operational endpoints (clock, task completion)."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Actor, get_current_coordinator
from app.db import get_db
from app.deps import get_clock
from app.enums import AssignmentStatus, EventType, TaskStatus
from app.models import Assignment, AssignmentEvent, Task
from app.schemas import EventOut
from app.services import time_utils
from app.services.errors import ConflictError

router = APIRouter(prefix="/api", tags=["operations"])


@router.get("/tasks/{task_id}/history", response_model=list[EventOut])
def task_history(
    task_id: int,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_coordinator),
) -> list[AssignmentEvent]:
    actor.ensure_can_access_task(db, task_id)
    return list(
        db.scalars(
            select(AssignmentEvent)
            .where(AssignmentEvent.task_id == task_id)
            .order_by(AssignmentEvent.id)
        )
    )


@router.get("/events", response_model=list[EventOut])
def all_events(
    limit: int = 200,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_coordinator),
) -> list[AssignmentEvent]:
    rows = db.scalars(
        select(AssignmentEvent).order_by(AssignmentEvent.id.desc()).limit(limit)
    ).all()
    # Coordinators see events for their own actions plus events on tasks in
    # their units.
    from app.models import CarePlan

    allowed_plans = {
        p.id
        for p in db.scalars(select(CarePlan)).all()
        if p.unit_id in actor.unit_ids()
    }
    out = []
    for row in rows:
        task = db.get(Task, row.task_id) if row.task_id else None
        if task is not None and task.plan_id in allowed_plans:
            out.append(row)
        elif task is None and (
            row.coordinator_id == actor.id
            or row.worker_id
            in {
                w.id
                for u in actor.coordinator.units
                for w in u.workers
            }
        ):
            out.append(row)
    return out[:limit]


@router.post("/tasks/{task_id}/complete")
def complete_task(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_coordinator),
) -> dict:
    """Mark an assigned task completed (used by care staff in the real system).

    Completion unlocks dependent tasks' prerequisite gate.
    """
    actor.ensure_can_access_task(db, task_id)
    task = db.get(Task, task_id)
    accepted = db.scalar(
        select(Assignment).where(
            Assignment.task_id == task_id,
            Assignment.status == AssignmentStatus.ACCEPTED.value,
        )
    )
    if accepted is None:
        raise ConflictError("Only an accepted task can be completed")
    task.status = TaskStatus.COMPLETED.value
    db.add(
        AssignmentEvent(
            task_id=task.id,
            assignment_id=accepted.id,
            worker_id=accepted.worker_id,
            coordinator_id=actor.id,
            event_type=EventType.INVITATION_ACCEPTED,
            detail="task_completed",
            created_at=get_clock(request).now(),
        )
    )
    db.commit()
    return {"task_id": task.id, "status": task.status.value}


@router.get("/clock")
def read_clock(request: Request) -> dict:
    """Inspect the controllable clock (useful in demos and tests)."""
    return {"now": get_clock(request).now().isoformat()}


@router.post("/clock/advance")
def advance_clock(
    request: Request,
    minutes: int = 0,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_coordinator),
) -> dict:
    """Advance a FakeClock and run the expiry sweep at the new time.

    Only meaningful when the app runs with a FakeClock (tests/demo). In
    production the SystemClock ignores this and returns the real time.
    """
    from app.clock import FakeClock

    clock = get_clock(request)
    if isinstance(clock, FakeClock) and minutes:
        clock.advance(minutes=minutes)
        from app.services import scheduling

        scheduling.sweep_and_reallocate(db, clock)
        db.commit()
    return {"now": clock.now().isoformat()}
