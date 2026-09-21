"""Worker-facing endpoints: invitation accept/decline and task lifecycle."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.clock import Clock
from app.config import Settings
from app.db import get_session
from app.deps import current_worker, get_clock, get_settings_dep
from app.models import Task, Worker
from app.schemas import TaskOut
from app.services.allocation import (
    AllocationError,
    accept_invitation,
    complete_task,
    decline_invitation,
    start_task,
)
from app.api.tasks import _allocation_error_response, _to_task_out

router = APIRouter(prefix="/worker", tags=["worker"])


@router.post("/invitations/{invitation_id}/accept", response_model=TaskOut)
def accept(
    invitation_id: int,
    db: Session = Depends(get_session),
    worker: Worker = Depends(current_worker),
    clock: Clock = Depends(get_clock),
) -> TaskOut:
    try:
        assignment = accept_invitation(db, invitation_id, worker.id, now=clock.now())
    except AllocationError as exc:
        raise _allocation_error_response(exc)
    return _to_task_out(db, db.get(Task, assignment.task_id))


@router.post("/invitations/{invitation_id}/decline", status_code=204)
def decline(
    invitation_id: int,
    db: Session = Depends(get_session),
    worker: Worker = Depends(current_worker),
    clock: Clock = Depends(get_clock),
    settings: Settings = Depends(get_settings_dep),
) -> None:
    try:
        decline_invitation(db, invitation_id, worker.id, now=clock.now(), settings=settings)
    except AllocationError as exc:
        raise _allocation_error_response(exc)


@router.post("/tasks/{task_id}/start", response_model=TaskOut)
def start(
    task_id: int,
    db: Session = Depends(get_session),
    worker: Worker = Depends(current_worker),
    clock: Clock = Depends(get_clock),
) -> TaskOut:
    try:
        start_task(db, task_id, now=clock.now(), actor_id=str(worker.id))
    except AllocationError as exc:
        raise _allocation_error_response(exc)
    return _to_task_out(db, db.get(Task, task_id))


@router.post("/tasks/{task_id}/complete", response_model=TaskOut)
def complete(
    task_id: int,
    db: Session = Depends(get_session),
    worker: Worker = Depends(current_worker),
    clock: Clock = Depends(get_clock),
) -> TaskOut:
    try:
        complete_task(db, task_id, now=clock.now(), actor_id=str(worker.id))
    except AllocationError as exc:
        raise _allocation_error_response(exc)
    return _to_task_out(db, db.get(Task, task_id))
