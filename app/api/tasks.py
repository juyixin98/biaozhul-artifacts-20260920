"""Task allocation, invitation lifecycle, reaping and manual adjustments."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import coordinator_id, require_coordinator, require_worker
from app.api.serializers import allocation_out, assignment_out, event_out, task_out
from app.database import get_db
from app.models import Assignment, AssignmentEvent, AssignmentStatus, Task, TaskStatus
from app.schemas import (
    AcceptIn,
    AcceptOut,
    AllocateResult,
    AssignmentOut,
    CancelIn,
    ConstraintViolation,
    ManualAssignIn,
    ReaperOut,
    RescheduleIn,
    TaskOut,
)
from app.services import assignments as svc
from app.services import tasks as tasks_svc

router = APIRouter(tags=["tasks"])


def _conflict(result: AllocateResult) -> HTTPException:
    # model_dump(mode="json") turns datetimes/enums into JSON-safe values.
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=result.model_dump(mode="json"),
    )


def _get_task_or_404(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


# --- queries ----------------------------------------------------------------


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks(
    plan_id: int | None = None,
    unit_id: int | None = None,
    status_filter: TaskStatus | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
):
    stmt = select(Task).order_by(Task.starts_at, Task.id)
    if plan_id is not None:
        stmt = stmt.where(Task.plan_id == plan_id)
    if unit_id is not None:
        stmt = stmt.where(Task.unit_id == unit_id)
    if status_filter is not None:
        stmt = stmt.where(Task.status == status_filter)
    return [task_out(t) for t in db.scalars(stmt)]


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: int, db: Session = Depends(get_db)):
    return task_out(_get_task_or_404(db, task_id))


@router.get("/tasks/{task_id}/events", response_model=list)
def task_events(
    task_id: int,
    db: Session = Depends(get_db),
    coordinator=Depends(require_coordinator),
):
    _get_task_or_404(db, task_id)
    rows = db.scalars(
        select(AssignmentEvent)
        .where(AssignmentEvent.task_id == task_id)
        .order_by(AssignmentEvent.created_at, AssignmentEvent.id)
    )
    return [event_out(e) for e in rows]


@router.get("/workers/me/invitations", response_model=list[AssignmentOut])
def my_invitations(worker=Depends(require_worker), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Assignment)
        .where(
            Assignment.worker_id == worker.id,
            Assignment.status == AssignmentStatus.invited,
        )
        .order_by(Assignment.expires_at)
    )
    return [assignment_out(a) for a in rows]


# --- automatic allocation ---------------------------------------------------


@router.post("/tasks/{task_id}/allocate", response_model=AllocateResult)
def allocate(
    task_id: int,
    db: Session = Depends(get_db),
    coordinator=Depends(require_coordinator),
):
    task = _get_task_or_404(db, task_id)
    try:
        outcome = svc.allocate_task(
            db, task, actor=f"coordinator:{coordinator.id}",
            coordinator_id=coordinator.id,
        )
    except svc.AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    result = allocation_out(outcome)
    if not result.allocated:
        # 409 carries the specific unsatisfied constraints without forcing work.
        raise _conflict(result)
    return result


@router.post("/admin/reap-expired", response_model=ReaperOut)
def reap_expired(
    auto_reallocate: bool = True,
    db: Session = Depends(get_db),
    cid: int | None = Depends(coordinator_id),
):
    """Expire invitations past their 8-minute TTL and re-offer their tasks."""
    outcomes = svc.reap_expired_invitations(
        db, coordinator_id=cid, auto_reallocate=auto_reallocate
    )
    return ReaperOut(
        expired=len(outcomes),
        reallocated=sum(1 for o in outcomes if o.allocated),
        results=[allocation_out(o) for o in outcomes],
    )


# --- worker invitation actions ----------------------------------------------


@router.post("/tasks/{task_id}/accept", response_model=AcceptOut)
def accept(
    task_id: int,
    payload: AcceptIn,
    worker=Depends(require_worker),
    db: Session = Depends(get_db),
):
    _get_task_or_404(db, task_id)
    assignment, violation, replayed = svc.accept_invitation(
        db, task_id, worker.id, request_key=payload.request_key
    )
    return AcceptOut(
        accepted=assignment is not None and not violation,
        assignment=assignment_out(assignment),
        violation=ConstraintViolation(
            code=violation.code, message=violation.message, detail=violation.detail
        ) if violation else None,
        replayed=replayed,
    )


@router.post("/tasks/{task_id}/decline", response_model=AssignmentOut)
def decline(
    task_id: int,
    worker=Depends(require_worker),
    db: Session = Depends(get_db),
):
    _get_task_or_404(db, task_id)
    assignment = svc.decline_invitation(db, task_id, worker.id)
    if assignment is None:
        raise HTTPException(status_code=409, detail="no live invitation for this worker")
    return assignment_out(assignment)


# --- manual coordinator adjustments -----------------------------------------


@router.post("/tasks/{task_id}/manual-assign", response_model=AllocateResult)
def manual_assign(
    task_id: int,
    payload: ManualAssignIn,
    db: Session = Depends(get_db),
    coordinator=Depends(require_coordinator),
):
    try:
        outcome = svc.manual_assign(
            db, task_id, payload.worker_id, reason=payload.reason,
            coordinator_id=coordinator.id,
        )
    except svc.AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except svc.TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except svc.ManualError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    result = allocation_out(outcome)
    if not result.allocated:
        raise _conflict(result)
    return result


@router.post("/tasks/{task_id}/reschedule", response_model=AllocateResult)
def reschedule(
    task_id: int,
    payload: RescheduleIn,
    db: Session = Depends(get_db),
    coordinator=Depends(require_coordinator),
):
    try:
        outcome = svc.reschedule_task(
            db, task_id, payload.starts_at, payload.ends_at,
            reason=payload.reason, coordinator_id=coordinator.id,
        )
    except svc.AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except svc.TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except svc.ManualError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    result = allocation_out(outcome)
    if not result.allocated:
        raise _conflict(result)
    return result


@router.post("/tasks/{task_id}/cancel", response_model=TaskOut)
def cancel(
    task_id: int,
    payload: CancelIn,
    db: Session = Depends(get_db),
    coordinator=Depends(require_coordinator),
):
    try:
        task = svc.cancel_task(
            db, task_id, reason=payload.reason, coordinator_id=coordinator.id
        )
    except svc.AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return task_out(task)


@router.post("/tasks/{task_id}/complete", response_model=TaskOut)
def complete(
    task_id: int,
    db: Session = Depends(get_db),
    coordinator=Depends(require_coordinator),
):
    try:
        task = tasks_svc.complete_task(db, task_id, coordinator_id=coordinator.id)
    except tasks_svc.AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except tasks_svc.TaskStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return task_out(task)
