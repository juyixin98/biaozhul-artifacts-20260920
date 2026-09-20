"""Scheduling endpoints: auto-assign, candidates, accept/decline, manual ops."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth import Actor, get_current_coordinator, get_worker
from app.db import get_db
from app.deps import get_clock, serialize_assignment
from app.models import Worker
from app.schemas import (
    AcceptResponse,
    AssignmentOut,
    CandidateOut,
    ManualAssignIn,
    ManualRescheduleIn,
    ManualUnassignIn,
    ScheduleResponse,
    TaskOut,
)
from app.services import scheduling
from app.services.constraints import evaluate_task_candidates
from app.deps import serialize_task

router = APIRouter(prefix="/api", tags=["scheduling"])


# ---- coordinator-driven automatic scheduling --------------------------------

@router.post("/tasks/{task_id}/schedule", response_model=ScheduleResponse)
def schedule(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_coordinator),
) -> dict:
    actor.ensure_can_access_task(db, task_id)
    result = scheduling.schedule_task(
        db, get_clock(request), task_id, coordinator_id=actor.id
    )
    db.commit()
    return result.to_dict()


@router.post("/scheduler/sweep", response_model=list[ScheduleResponse])
def sweep(
    request: Request,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_coordinator),
) -> list[dict]:
    """Expire all invitations past their 8-minute TTL and re-allocate.

    The same sweep runs automatically on every schedule/accept call; this
    endpoint lets the background job / an operator trigger it explicitly.
    """
    results = scheduling.sweep_and_reallocate(db, get_clock(request))
    db.commit()
    return [r.to_dict() for r in results]


@router.get("/tasks/{task_id}/candidates", response_model=list[CandidateOut])
def candidates(
    task_id: int,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_coordinator),
) -> list[dict]:
    """Return the full evaluation table, including *why* each worker fails.

    When nobody is feasible the coordinator UI shows the concrete violated
    constraints instead of a silent forced placement.
    """
    actor.ensure_can_access_task(db, task_id)
    from app.models import Task

    task = db.get(Task, task_id)
    evaluations = evaluate_task_candidates(db, task)
    out = []
    for e in evaluations:
        out.append(
            {
                "worker_id": e.worker_id,
                "worker_name": e.worker_name,
                "feasible": e.feasible,
                "violations": [v.to_dict() for v in e.violations],
                "min_remaining_minutes": e.min_remaining_minutes,
                "total_load_minutes": e.total_load_minutes,
            }
        )
    return out


# ---- worker invitation responses --------------------------------------------

@router.post(
    "/assignments/{assignment_id}/accept", response_model=AcceptResponse
)
def accept(
    assignment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    worker: Worker = Depends(get_worker),
) -> dict:
    asm = scheduling.accept_invitation(
        db,
        get_clock(request),
        assignment_id,
        worker_external_id=worker.external_id,
    )
    db.commit()
    return {
        "assignment": serialize_assignment(asm),
        "task_status": asm.task.status.value
        if hasattr(asm.task.status, "value")
        else asm.task.status,
    }


@router.post("/assignments/{assignment_id}/decline", response_model=AssignmentOut)
def decline(
    assignment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    worker: Worker = Depends(get_worker),
) -> dict:
    asm = scheduling.decline_invitation(
        db,
        get_clock(request),
        assignment_id,
        worker_external_id=worker.external_id,
    )
    db.commit()
    return serialize_assignment(asm)


# ---- manual coordinator actions (same constraint checks) --------------------

@router.post("/tasks/{task_id}/manual-assign", response_model=ScheduleResponse)
def manual_assign(
    task_id: int,
    payload: ManualAssignIn,
    request: Request,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_coordinator),
) -> dict:
    actor.ensure_can_access_task(db, task_id)
    result = scheduling.manual_assign(
        db,
        get_clock(request),
        task_id=task_id,
        worker_id=payload.worker_id,
        coordinator_id=actor.id,
        reason=payload.reason,
    )
    db.commit()
    return result.to_dict()


@router.post("/tasks/{task_id}/manual-unassign", status_code=200)
def manual_unassign(
    task_id: int,
    payload: ManualUnassignIn,
    request: Request,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_coordinator),
) -> dict:
    actor.ensure_can_access_task(db, task_id)
    scheduling.manual_unassign(
        db,
        get_clock(request),
        task_id=task_id,
        coordinator_id=actor.id,
        reason=payload.reason,
    )
    db.commit()
    return {"task_id": task_id, "status": "pending"}


@router.post("/tasks/{task_id}/manual-reschedule", response_model=TaskOut)
def manual_reschedule(
    task_id: int,
    payload: ManualRescheduleIn,
    request: Request,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_coordinator),
) -> dict:
    actor.ensure_can_access_task(db, task_id)
    task = scheduling.manual_reschedule(
        db,
        get_clock(request),
        task_id=task_id,
        starts_at=payload.starts_at,
        duration_minutes=payload.duration_minutes,
        coordinator_id=actor.id,
        reason=payload.reason,
    )
    db.commit()
    return serialize_task(task)
