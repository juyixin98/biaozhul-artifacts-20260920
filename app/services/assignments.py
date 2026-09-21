"""Allocation, invitation acceptance, expiry reaping and manual adjustments.

Concurrency model (PostgreSQL):

* Every path that reads or mutates a task's live assignment locks the task row
  first (``SELECT ... FOR UPDATE``). Combined with the partial unique index
  ``uq_assignment_active_task`` this guarantees only one live invitation or
  booking per task even when multiple workers accept at once.
* Weekly capacity counts ``invited`` and ``assigned`` shifts, so an
  outstanding offer already reserves the worker's hours. Lapsing the offer
  releases them — never more than one live row per task, so hours can never be
  double counted by duplicate accepts.
* Accept calls may carry an idempotency key; a repeated key replays the stored
  outcome instead of re-running the booking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clock import clock
from app.config import get_settings
from app.models import (
    Assignment,
    AssignmentEvent,
    AssignmentStatus,
    CarePlan,
    CareWorker,
    Coordinator,
    IdempotentRequest,
    Task,
    TaskStatus,
)
from app.services.constraints import Violation, check_constraints, rank_candidates


class TaskNotFoundError(KeyError):
    pass


class AuthorizationError(PermissionError):
    pass


@dataclass
class AllocationOutcome:
    task: Task
    assignment: Assignment | None = None
    violations: list[Violation] = field(default_factory=list)

    @property
    def allocated(self) -> bool:
        return self.assignment is not None


def _event(db, *, task_id, action, actor, assignment_id=None, worker_id=None,
           reason=None, detail=None) -> None:
    db.add(
        AssignmentEvent(
            assignment_id=assignment_id,
            task_id=task_id,
            worker_id=worker_id,
            action=action,
            reason=reason,
            actor=actor,
            detail=detail or {},
            created_at=clock.now(),
        )
    )


def _lock_task(db: Session, task_id: int) -> Task:
    task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None:
        raise TaskNotFoundError(f"task {task_id} not found")
    return task


def _active_assignment(db: Session, task_id: int, lock: bool = True):
    stmt = select(Assignment).where(
        Assignment.task_id == task_id,
        Assignment.status.in_((AssignmentStatus.invited, AssignmentStatus.assigned)),
    )
    if lock:
        stmt = stmt.with_for_update(of=Assignment)
    return db.scalars(stmt).first()


def _unit_workers(db: Session, unit_id: int) -> list[CareWorker]:
    return list(
        db.scalars(
            select(CareWorker)
            .where(CareWorker.unit_id == unit_id, CareWorker.active.is_(True))
            .order_by(CareWorker.id)
        )
    )


def authorize_unit(db: Session, coordinator_id: int | None, unit_id: int) -> None:
    settings = get_settings()
    if not settings.require_coordinator or coordinator_id is None:
        return
    coordinator = db.get(Coordinator, coordinator_id)
    if coordinator is None:
        raise AuthorizationError(f"coordinator {coordinator_id} not found")
    if not any(u.id == unit_id for u in coordinator.units):
        raise AuthorizationError(
            f"coordinator {coordinator_id} is not authorised for unit {unit_id}"
        )


# --- automatic allocation ---------------------------------------------------


def allocate_task(
    db: Session,
    task: Task,
    *,
    actor: str = "system",
    coordinator_id: int | None = None,
    reoffer_worker_ids: set[int] | None = None,
) -> AllocationOutcome:
    """Offer the task to the best feasible candidate.

    Locks the task; if a live offer/booking already exists it is a no-op.
    When nobody fits, every blocking constraint of the *best* (closest)
    candidate is returned instead of forcing a booking.
    """
    authorize_unit(db, coordinator_id, task.unit_id)
    locked = _lock_task(db, task.id)
    if locked.status in (TaskStatus.assigned, TaskStatus.completed, TaskStatus.cancelled):
        return AllocationOutcome(task=locked, assignment=_active_assignment(db, locked.id))
    existing = _active_assignment(db, locked.id)
    if existing is not None:
        return AllocationOutcome(task=locked, assignment=existing)

    workers = _unit_workers(db, locked.unit_id)
    if reoffer_worker_ids:
        workers = [w for w in workers if w.id not in reoffer_worker_ids]

    ranked = rank_candidates(db, workers, locked)
    feasible = [pair for pair in ranked if pair[1].feasible]
    if not feasible:
        best = ranked[0] if ranked else None
        violations = best[1].violations if best else [
            Violation("no_workers", "No active workers in the task's unit",
                      {"unit_id": locked.unit_id})
        ]
        _event(db, task_id=locked.id, action="allocation_failed", actor=actor,
               detail={"violations": [v.__dict__ for v in violations]})
        db.commit()
        return AllocationOutcome(task=locked, violations=violations)

    worker, score = feasible[0]
    now = clock.now()
    ttl_minutes = get_settings().invitation_ttl_minutes
    assignment = Assignment(
        task_id=locked.id,
        worker_id=worker.id,
        status=AssignmentStatus.invited,
        invited_at=now,
        expires_at=now + timedelta(minutes=ttl_minutes),
        created_via="auto",
        created_by_coordinator_id=coordinator_id,
        created_at=now,
    )
    locked.status = TaskStatus.invited
    locked.updated_at = now
    db.add(assignment)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        # Lost a race to another live offer; surface it instead of duplicating.
        locked = _lock_task(db, task.id)
        return AllocationOutcome(task=locked, assignment=_active_assignment(db, locked.id))

    _event(
        db, task_id=locked.id, assignment_id=assignment.id, worker_id=worker.id,
        action="invitation_created", actor=actor,
        detail={"rank_remaining_minutes": score.remaining_minutes,
                "rank_load_minutes": score.load_minutes},
    )
    db.commit()
    db.refresh(assignment)
    return AllocationOutcome(task=locked, assignment=assignment)


# --- acceptance -------------------------------------------------------------


def accept_invitation(
    db: Session,
    task_id: int,
    worker_id: int,
    *,
    request_key: str | None = None,
) -> tuple[Assignment | None, Violation | None, bool]:
    """Worker accepts an outstanding invitation.

    Returns (assignment, violation, replayed). Exactly one concurrent accept
    can succeed: the task lock and the partial unique index reject the loser,
    and a re-check of every constraint (overlap/rest/weekly cap/qualification)
    guards against an accept racing another booking or an expiring offer.
    """
    now = clock.now()

    if request_key is not None:
        stored = db.get(IdempotentRequest, request_key)
        if stored is not None:
            assignment = (
                db.get(Assignment, stored.assignment_id)
                if stored.assignment_id else None
            )
            return assignment, None, True

    task = _lock_task(db, task_id)
    assignment = _active_assignment(db, task_id)
    if assignment is None:
        return None, Violation("no_active_offer", "No live invitation for this task",
                               {"task_id": task_id}), False
    if assignment.worker_id != worker_id:
        return None, Violation(
            "offer_belongs_to_other_worker",
            "The live invitation is addressed to another worker",
            {"task_id": task_id, "invited_worker_id": assignment.worker_id},
        ), False
    if assignment.status == AssignmentStatus.assigned:
        return assignment, None, True
    if assignment.expires_at <= now:
        # Accept raced the reaper; treat as expired so the task can re-offer.
        _expire_locked(db, task, assignment, actor=f"worker:{worker_id}",
                       reason="accepted_after_expiry")
        db.commit()
        return None, Violation("invitation_expired",
                               "Invitation had already expired at accept time",
                               {"expires_at": assignment.expires_at.isoformat()}), False

    worker = db.get(CareWorker, worker_id)
    violations = check_constraints(
        db, worker, task, exclude_assignment_id=assignment.id
    )
    if violations:
        # Something changed since the offer (e.g. another booking). Lapse and
        # report the conflict rather than overbooking the worker.
        _expire_locked(db, task, assignment, actor=f"worker:{worker_id}",
                       reason="constraints_no_longer_satisfied")
        db.commit()
        return None, violations[0], False

    assignment.status = AssignmentStatus.assigned
    assignment.responded_at = now
    task.status = TaskStatus.assigned
    task.updated_at = now
    _event(db, task_id=task.id, assignment_id=assignment.id, worker_id=worker_id,
           action="invitation_accepted", actor=f"worker:{worker_id}")
    db.flush()

    if request_key is not None:
        # Record the key against the now-committed booking; a duplicate request
        # key racing us hits the PK and the winner's result is replayed.
        db.add(IdempotentRequest(
            request_key=request_key, worker_id=worker_id,
            assignment_id=assignment.id, outcome="accepted", created_at=now,
        ))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        stored = db.get(IdempotentRequest, request_key) if request_key else None
        if stored is not None:
            return db.get(Assignment, stored.assignment_id), None, True
        raise
    db.refresh(assignment)
    return assignment, None, False


def decline_invitation(db: Session, task_id: int, worker_id: int) -> Assignment | None:
    task = _lock_task(db, task_id)
    assignment = _active_assignment(db, task_id)
    if assignment is None or assignment.worker_id != worker_id:
        db.rollback()
        return None
    assignment.status = AssignmentStatus.declined
    assignment.responded_at = clock.now()
    task.status = TaskStatus.pending
    _event(db, task_id=task.id, assignment_id=assignment.id, worker_id=worker_id,
           action="invitation_declined", actor=f"worker:{worker_id}")
    db.commit()
    db.refresh(assignment)
    return assignment


def _expire_locked(db: Session, task: Task, assignment: Assignment, *,
                   actor: str, reason: str) -> None:
    assignment.status = AssignmentStatus.expired
    assignment.responded_at = clock.now()
    task.status = TaskStatus.pending
    task.updated_at = clock.now()
    _event(db, task_id=task.id, assignment_id=assignment.id,
           worker_id=assignment.worker_id, action="invitation_expired",
           actor=actor, reason=reason)


# --- timeout reaper ---------------------------------------------------------


def reap_expired_invitations(
    db: Session, *, coordinator_id: int | None = None, auto_reallocate: bool = True
) -> list[AllocationOutcome]:
    """Expire every invitation past its 8-minute deadline.

    Expired invitations are released and the task is offered to the next
    eligible worker, excluding the worker who let it lapse. The same task lock
    and constraint re-check make ``accept`` vs ``reap`` races safe: whichever
    commits first wins, the loser observes the new state and backs off.
    """
    now = clock.now()
    stale = db.scalars(
        select(Assignment)
        .where(
            Assignment.status == AssignmentStatus.invited,
            Assignment.expires_at <= now,
        )
        .order_by(Assignment.id)
    ).all()

    outcomes: list[AllocationOutcome] = []
    for stale_assignment in stale:
        task = _lock_task(db, stale_assignment.task_id)
        assignment = db.scalars(
            select(Assignment)
            .where(Assignment.id == stale_assignment.id)
            .with_for_update(of=Assignment)
        ).first()
        if assignment is None or assignment.status != AssignmentStatus.invited:
            db.rollback()
            continue
        lapsed_worker = assignment.worker_id
        _expire_locked(db, task, assignment, actor="system",
                       reason="ttl_expired")
        db.commit()

        if auto_reallocate:
            outcome = allocate_task(
                db, task, actor="system", coordinator_id=coordinator_id,
                reoffer_worker_ids={lapsed_worker},
            )
            outcomes.append(outcome)
        else:
            outcomes.append(AllocationOutcome(task=task))
    return outcomes


# --- manual adjustments -----------------------------------------------------


def manual_assign(
    db: Session,
    task_id: int,
    worker_id: int,
    *,
    reason: str,
    coordinator_id: int,
) -> AllocationOutcome:
    """Coordinator picks a specific worker. Goes through the same constraint
    checks; violations block the action instead of forcing the booking."""
    task = _lock_task(db, task_id)
    authorize_unit(db, coordinator_id, task.unit_id)
    worker = db.get(CareWorker, worker_id)
    if worker is None:
        raise TaskNotFoundError(f"worker {worker_id} not found")

    existing = _active_assignment(db, task_id)
    if existing is not None and existing.status == AssignmentStatus.assigned:
        raise _manual_error("task already assigned; cancel it first", task)
    violations = check_constraints(
        db, worker, task,
        exclude_assignment_id=existing.id if existing else None,
    )
    if violations:
        _event(db, task_id=task.id, action="manual_assign_rejected",
               actor=f"coordinator:{coordinator_id}", worker_id=worker_id,
               reason=reason, detail={"violations": [v.__dict__ for v in violations]})
        db.commit()
        return AllocationOutcome(task=task, violations=violations)

    now = clock.now()
    if existing is not None:
        # Outstanding invitation displaced by an explicit coordinator choice.
        existing.status = AssignmentStatus.cancelled
        existing.responded_at = now

    assignment = Assignment(
        task_id=task.id,
        worker_id=worker_id,
        status=AssignmentStatus.assigned,
        invited_at=now,
        expires_at=now,
        responded_at=now,
        created_via="manual",
        created_by_coordinator_id=coordinator_id,
        created_at=now,
    )
    task.status = TaskStatus.assigned
    task.updated_at = now
    db.add(assignment)
    db.flush()
    _event(db, task_id=task.id, assignment_id=assignment.id, worker_id=worker_id,
           action="manual_assign", actor=f"coordinator:{coordinator_id}", reason=reason)
    db.commit()
    db.refresh(assignment)
    return AllocationOutcome(task=task, assignment=assignment)


class ManualError(RuntimeError):
    pass


def _manual_error(message: str, task: Task) -> ManualError:
    return ManualError(message)


def cancel_task(db: Session, task_id: int, *, reason: str, coordinator_id: int) -> Task:
    task = _lock_task(db, task_id)
    authorize_unit(db, coordinator_id, task.unit_id)
    live = _active_assignment(db, task_id)
    if live is not None:
        live.status = AssignmentStatus.cancelled
        live.responded_at = clock.now()
    task.status = TaskStatus.cancelled
    task.updated_at = clock.now()
    _event(db, task_id=task.id, assignment_id=live.id if live else None,
           worker_id=live.worker_id if live else None,
           action="manual_cancel", actor=f"coordinator:{coordinator_id}", reason=reason)
    db.commit()
    db.refresh(task)
    return task


def reschedule_task(
    db: Session, task_id: int, starts_at, ends_at, *,
    reason: str, coordinator_id: int,
) -> AllocationOutcome:
    """Move an unstarted task. Assigned bookings are released to pending and
    re-allocated after the same constraint checks against the new window."""
    task = _lock_task(db, task_id)
    authorize_unit(db, coordinator_id, task.unit_id)
    if ends_at <= starts_at:
        raise ManualError("ends_at must be after starts_at")
    if task.status in (TaskStatus.completed, TaskStatus.cancelled):
        raise ManualError(f"cannot reschedule a {task.status.value} task")

    plan = db.get(CarePlan, task.plan_id)
    zone = plan.service_timezone
    from app.timeutils import get_zone, utc_in_zone
    local_start = utc_in_zone(starts_at, get_zone(zone))
    local_end = utc_in_zone(ends_at, get_zone(zone))
    if local_start.date().isoformat() != task.occurrence_key:
        raise ManualError("rescheduling must stay on the task's service date")

    live = _active_assignment(db, task_id)
    released_worker = None
    if live is not None:
        released_worker = live.worker_id
        live.status = AssignmentStatus.cancelled
        live.responded_at = clock.now()

    task.starts_at = starts_at
    task.ends_at = ends_at
    task.status = TaskStatus.pending
    task.updated_at = clock.now()
    _event(db, task_id=task.id, assignment_id=live.id if live else None,
           worker_id=released_worker, action="manual_reschedule",
           actor=f"coordinator:{coordinator_id}", reason=reason,
           detail={"starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat()})
    db.commit()

    return allocate_task(
        db, task, actor=f"coordinator:{coordinator_id}",
        coordinator_id=coordinator_id,
        reoffer_worker_ids={released_worker} if released_worker else None,
    )
