"""Task endpoints: search, allocation, manual adjustment, audit, weekly hours."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import Clock
from app.config import Settings
from app.db import get_session
from app.deps import (
    assert_unit_authorized,
    current_coordinator,
    get_clock,
    get_settings_dep,
)
from app.models import (
    Assignment,
    AuditLog,
    Invitation,
    InvitationStatus,
    LOAD_BEARING_STATUSES,
    Task,
    TaskStatus,
    Worker,
)
from app.schemas import (
    AllocateOut,
    AuditOut,
    BulkAllocateOut,
    InvitationOut,
    ManualAssignIn,
    ManualRescheduleIn,
    ReasonIn,
    TaskOut,
    WeeklyHoursOut,
)
from app.services.allocation import (
    AllocationError,
    allocate_task,
    choose_candidate,
    expire_due_invitations,
    manual_assign,
    manual_reschedule,
    manual_unassign,
)
from app.services.timeutils import as_utc, split_into_weeks

router = APIRouter(tags=["tasks"])


def _active_assignment(db: Session, task_id: int) -> Assignment | None:
    return db.scalar(
        select(Assignment).where(Assignment.task_id == task_id)
    )


def _to_task_out(db: Session, task: Task) -> TaskOut:
    assignment = _active_assignment(db, task.id)
    invitation = db.scalar(
        select(Invitation).where(
            Invitation.task_id == task.id,
            Invitation.status == InvitationStatus.PENDING.value,
        )
    )
    return TaskOut(
        id=task.id,
        plan_id=task.plan_id,
        unit_id=task.unit_id,
        scheduled_date=task.scheduled_date,
        scheduled_start=task.scheduled_start,
        scheduled_end=task.scheduled_end,
        status=task.status,
        qualification_codes=task.qualification_codes,
        prerequisites=task.prerequisites,
        assigned_worker_id=assignment.worker_id if assignment else None,
        active_invitation_id=invitation.id if invitation else None,
    )


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks(
    status: str | None = None,
    unit_id: int | None = None,
    plan_id: int | None = None,
    limit: int = Query(default=200, le=1000),
    db: Session = Depends(get_session),
) -> list[TaskOut]:
    stmt = select(Task).order_by(Task.scheduled_start, Task.id).limit(limit)
    if status:
        if status not in {s.value for s in TaskStatus}:
            raise HTTPException(422, f"unknown status {status}")
        stmt = stmt.where(Task.status == status)
    if unit_id is not None:
        stmt = stmt.where(Task.unit_id == unit_id)
    if plan_id is not None:
        stmt = stmt.where(Task.plan_id == plan_id)
    tasks = list(db.scalars(stmt))
    return [_to_task_out(db, t) for t in tasks]


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: int, db: Session = Depends(get_session)) -> TaskOut:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    return _to_task_out(db, task)


@router.get("/tasks/{task_id}/invitations", response_model=list[InvitationOut])
def list_invitations(task_id: int, db: Session = Depends(get_session)) -> list[Invitation]:
    if db.get(Task, task_id) is None:
        raise HTTPException(404, "task not found")
    return list(db.scalars(
        select(Invitation).where(Invitation.task_id == task_id).order_by(Invitation.round, Invitation.id)
    ))


@router.get("/tasks/{task_id}/history", response_model=list[AuditOut])
def task_history(task_id: int, db: Session = Depends(get_session)) -> list[AuditLog]:
    if db.get(Task, task_id) is None:
        raise HTTPException(404, "task not found")
    return list(db.scalars(
        select(AuditLog).where(AuditLog.task_id == task_id).order_by(AuditLog.id)
    ))


def _authorize_task(db: Session, coordinator, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    assert_unit_authorized(coordinator, task.unit_id)
    return task


def _allocation_error_response(exc: AllocationError) -> HTTPException:
    status_code = {
        "not_found": 404,
        "forbidden": 403,
        "task_not_open": 409,
        "already_assigned": 409,
        "already_accepted": 409,
        "invitation_expired": 409,
        "invitation_cancelled": 409,
        "invitation_declined": 409,
        "not_assigned": 409,
        "bad_interval": 422,
        "constraint_violation": 422,
    }.get(exc.code, 400)
    detail: dict = {"error": exc.code, "message": str(exc)}
    if exc.violations:
        detail["violations"] = [
            {"constraint": v.constraint, "message": v.message,
             "worker_id": v.worker_id, **({"detail": v.detail} if v.detail else {})}
            for v in exc.violations
        ]
    return HTTPException(status_code, detail)


@router.post("/tasks/{task_id}/allocate", response_model=AllocateOut)
def allocate_one(
    task_id: int,
    db: Session = Depends(get_session),
    coordinator=Depends(current_coordinator),
    clock: Clock = Depends(get_clock),
    settings: Settings = Depends(get_settings_dep),
) -> AllocateOut:
    _authorize_task(db, coordinator, task_id)
    try:
        result = allocate_task(db, task_id, now=clock.now(), settings=settings,
                               actor_id=str(coordinator.id))
    except AllocationError as exc:
        raise _allocation_error_response(exc)
    return AllocateOut(**result.__dict__)


@router.post("/tasks/allocate-due", response_model=BulkAllocateOut)
def allocate_due(
    db: Session = Depends(get_session),
    coordinator=Depends(current_coordinator),
    clock: Clock = Depends(get_clock),
    settings: Settings = Depends(get_settings_dep),
) -> BulkAllocateOut:
    """Expire due invitations (reallocating them) and invite for every open task."""
    expire_due_invitations(db, now=clock.now(), settings=settings)
    open_tasks = list(db.scalars(
        select(Task).where(
            Task.status.in_([TaskStatus.PLANNED.value, TaskStatus.UNASSIGNED.value])
        ).order_by(Task.scheduled_start)
    ))
    results = []
    for task in open_tasks:
        assert_unit_authorized(coordinator, task.unit_id)
        result = allocate_task(db, task.id, now=clock.now(), settings=settings,
                               actor_id=str(coordinator.id))
        results.append(AllocateOut(**result.__dict__))
    return BulkAllocateOut(results=results)


@router.post("/tasks/{task_id}/manual-assign", response_model=TaskOut)
def api_manual_assign(
    task_id: int,
    payload: ManualAssignIn,
    db: Session = Depends(get_session),
    coordinator=Depends(current_coordinator),
    clock: Clock = Depends(get_clock),
) -> TaskOut:
    _authorize_task(db, coordinator, task_id)
    try:
        manual_assign(db, task_id, payload.worker_id, now=clock.now(),
                      coordinator=coordinator, reason=payload.reason)
    except AllocationError as exc:
        raise _allocation_error_response(exc)
    return _to_task_out(db, db.get(Task, task_id))


@router.post("/tasks/{task_id}/manual-unassign", response_model=TaskOut)
def api_manual_unassign(
    task_id: int,
    payload: ReasonIn,
    db: Session = Depends(get_session),
    coordinator=Depends(current_coordinator),
    clock: Clock = Depends(get_clock),
) -> TaskOut:
    _authorize_task(db, coordinator, task_id)
    try:
        manual_unassign(db, task_id, now=clock.now(), coordinator=coordinator, reason=payload.reason)
    except AllocationError as exc:
        raise _allocation_error_response(exc)
    return _to_task_out(db, db.get(Task, task_id))


@router.post("/tasks/{task_id}/manual-reschedule", response_model=TaskOut)
def api_manual_reschedule(
    task_id: int,
    payload: ManualRescheduleIn,
    db: Session = Depends(get_session),
    coordinator=Depends(current_coordinator),
    clock: Clock = Depends(get_clock),
) -> TaskOut:
    _authorize_task(db, coordinator, task_id)
    try:
        manual_reschedule(
            db, task_id,
            new_start=as_utc(payload.scheduled_start),
            new_end=as_utc(payload.scheduled_end),
            now=clock.now(), coordinator=coordinator, reason=payload.reason,
        )
    except AllocationError as exc:
        raise _allocation_error_response(exc)
    return _to_task_out(db, db.get(Task, task_id))


@router.get("/tasks/{task_id}/candidates")
def preview_candidates(
    task_id: int,
    db: Session = Depends(get_session),
    coordinator=Depends(current_coordinator),
) -> dict:
    """Dry-run ranking: scores + the concrete constraints that disqualify each worker."""
    task = _authorize_task(db, coordinator, task_id)
    ranked = choose_candidate(db, task)
    return {
        "task_id": task.id,
        "candidates": [
            {
                "worker_id": rc.worker.id,
                "feasible": rc.feasible,
                "min_remaining_minutes": rc.min_remaining_minutes,
                "existing_load_minutes": rc.existing_load_minutes,
                "violations": [
                    {"constraint": v.constraint, "message": v.message,
                     **({"detail": v.detail} if v.detail else {})}
                    for v in rc.violations
                ],            }
            for rc in ranked
        ],
    }


@router.get("/workers/{worker_id}/weekly-hours", response_model=WeeklyHoursOut)
def worker_weekly_hours(
    worker_id: int,
    db: Session = Depends(get_session),
    coordinator=Depends(current_coordinator),
) -> WeeklyHoursOut:
    """Hours per ISO week (UTC Monday 00:00), with overnight shifts split across weeks."""
    worker = db.get(Worker, worker_id)
    if worker is None:
        raise HTTPException(404, "worker not found")
    assert_unit_authorized(coordinator, worker.unit_id)
    assignments = list(db.scalars(
        select(Assignment)
        .join(Task, Assignment.task_id == Task.id)
        .where(
            Assignment.worker_id == worker_id,
            Assignment.status.in_([s.value for s in LOAD_BEARING_STATUSES]),
        )
    ))
    weeks: dict[str, float] = {}
    for a in assignments:
        for piece in split_into_weeks(a.task.scheduled_start, a.task.scheduled_end):
            key = piece.week_start.isoformat()
            weeks[key] = weeks.get(key, 0.0) + piece.duration.total_seconds() / 3600
    return WeeklyHoursOut(worker_id=worker_id, weeks={k: round(v, 4) for k, v in sorted(weeks.items())})


@router.get("/audit", response_model=list[AuditOut])
def list_audit(
    task_id: int | None = None,
    limit: int = Query(default=200, le=1000),
    db: Session = Depends(get_session),
    coordinator=Depends(current_coordinator),
) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if task_id is not None:
        task = db.get(Task, task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        assert_unit_authorized(coordinator, task.unit_id)
        stmt = stmt.where(AuditLog.task_id == task_id)
    elif not coordinator.is_admin:
        # Non-admins see only audit rows for tasks in their authorized units.
        granted = [g.unit_id for g in coordinator.unit_grants]
        if not granted:
            return []
        stmt = stmt.join(Task, Task.id == AuditLog.task_id).where(Task.unit_id.in_(granted))
    return list(db.scalars(stmt))
