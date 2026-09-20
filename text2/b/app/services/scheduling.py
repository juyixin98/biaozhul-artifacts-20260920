"""Assignment orchestration: invitations, accepts, expiry and re-allocation.

Concurrency model
-----------------
All state-changing operations run inside one transaction and take a row lock
on the task (``SELECT ... FOR UPDATE``; a plain no-op ``SELECT`` on SQLite)
before inspecting or touching its assignments. Combined with the partial
unique indexes ``uq_one_pending_per_task`` / ``uq_one_accepted_per_task`` this
guarantees:

* two workers accepting the *same* invitation concurrently -> exactly one
  accepted row survives; the loser gets a 409 conflict;
* a timeout sweep racing an accept -> whichever commits first wins, the other
  sees the terminal state and does nothing;
* repeated requests never create a second invitation or double-count hours
  (live invitations already occupy the calendar in ``constraints.py``).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import Clock
from app.config import settings
from app.enums import AssignmentStatus, EventType, TaskStatus
from app.models import (
    Assignment,
    AssignmentEvent,
    Task,
    TaskPrerequisite,
    TaskQualification,
    Worker,
)
from app.services import constraints, time_utils
from app.services.errors import ConflictError, NotFoundError, ValidationError


def _log(db: Session, *, event_type, task_id=None, assignment_id=None,
         worker_id=None, coordinator_id=None, detail=None) -> None:
    db.add(
        AssignmentEvent(
            task_id=task_id,
            assignment_id=assignment_id,
            worker_id=worker_id,
            coordinator_id=coordinator_id,
            event_type=event_type,
            detail=json.dumps(detail) if not isinstance(detail, str | None) else detail,
            created_at=db.info.get("now") or datetime.now(time_utils.UTC),
        )
    )


def _lock_task(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise NotFoundError(f"Task {task_id} not found")
    if db.get_bind().dialect.name == "sqlite":
        # SQLite serialises writes; an explicit lock is unnecessary.
        db.flush()
    else:
        db.execute(select(Task).where(Task.id == task_id).with_for_update())
    return task


def _current_now(db: Session, clock: Clock) -> datetime:
    return db.info.get("now") or clock.now()


# ---------------------------------------------------------------------------
# Prerequisite gate
# ---------------------------------------------------------------------------

def unmet_prerequisites(db: Session, task: Task) -> list[int]:
    """Return prerequisite task ids that are not COMPLETED yet."""
    links = db.scalars(
        select(TaskPrerequisite).where(TaskPrerequisite.task_id == task.id)
    ).all()
    unmet: list[int] = []
    for link in links:
        required = db.get(Task, link.required_task_id)
        if required is None or required.status != TaskStatus.COMPLETED.value:
            unmet.append(link.required_task_id)
    return unmet


# ---------------------------------------------------------------------------
# Expiry sweep (timeout re-allocation)
# ---------------------------------------------------------------------------

def expire_due_invitations(
    db: Session, clock: Clock, *, task_id: int | None = None
) -> list[Assignment]:
    """Mark every due PENDING invitation EXPIRED.

    When ``task_id`` is given only that task is swept (the normal path before
    assigning/accepting); otherwise all due invitations are swept (the
    scheduled synchroniser). Returns the expired assignments; callers decide
    whether to re-allocate.
    """
    now = clock.now()
    db.info["now"] = now
    stmt = (
        select(Assignment)
        .where(Assignment.status == AssignmentStatus.PENDING.value)
        .where(Assignment.expires_at <= now)
    )
    if task_id is not None:
        stmt = stmt.where(Assignment.task_id == task_id)
    expired: list[Assignment] = []
    for asm in db.scalars(stmt):
        task = _lock_task(db, asm.task_id)
        # Re-read status under lock: an accept may have just committed.
        if asm.status != AssignmentStatus.PENDING.value:
            continue
        asm.status = AssignmentStatus.EXPIRED.value
        asm.responded_at = now
        if task.status == TaskStatus.INVITED.value:
            task.status = TaskStatus.PENDING.value
        _log(
            db,
            event_type=EventType.INVITATION_EXPIRED,
            task_id=task.id,
            assignment_id=asm.id,
            worker_id=asm.worker_id,
            detail=f"Invitation expired at {now.isoformat()}",
        )
        expired.append(asm)
    db.flush()
    return expired


# ---------------------------------------------------------------------------
# Candidate search
# ---------------------------------------------------------------------------

def evaluate_task_candidates(db: Session, task: Task) -> list[
    constraints.CandidateEvaluation
]:
    required_qids = [tq.qualification_id for tq in task.qualifications]
    workers = constraints.candidate_pool_for_unit(db, task.plan.unit_id)
    evaluations: list[constraints.CandidateEvaluation] = []
    for worker in workers:
        ev = constraints.evaluate_candidate(
            db,
            worker=worker,
            task=task,
            required_qualification_ids=required_qids,
        )
        evaluations.append(ev)
    return evaluations


# ---------------------------------------------------------------------------
# Invite / auto-schedule
# ---------------------------------------------------------------------------

class SchedulingResult:
    """Outcome of trying to schedule one task."""

    def __init__(
        self,
        *,
        task_id: int,
        status: str,
        assignment_id: int | None = None,
        worker_id: int | None = None,
        violations: list[dict] | None = None,
    ) -> None:
        self.task_id = task_id
        self.status = status  # invited | already_live | unsatisfiable
        self.assignment_id = assignment_id
        self.worker_id = worker_id
        self.violations = violations or []

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "assignment_id": self.assignment_id,
            "worker_id": self.worker_id,
            "violations": self.violations,
        }


def schedule_task(
    db: Session,
    clock: Clock,
    task_id: int,
    *,
    invited_by: str = "system",
    coordinator_id: int | None = None,
    ttl_minutes: int | None = None,
) -> SchedulingResult:
    """Invite the best feasible candidate for a task.

    Returns ``unsatisfiable`` with the full per-worker constraint report when
    nobody qualifies; the caller never gets a forced assignment.
    """
    now = clock.now()
    db.info["now"] = now
    task = _lock_task(db, task_id)

    # A concurrent/earlier call may already hold a live invitation.
    live = _live_assignment(db, task.id)
    if live is not None:
        return SchedulingResult(
            task_id=task.id,
            status="already_live",
            assignment_id=live.id,
            worker_id=live.worker_id,
        )

    # Expire any stale invitation before reasoning (cheap no-op when none).
    expire_due_invitations(db, clock, task_id=task.id)

    if task.status not in (TaskStatus.PENDING.value, TaskStatus.INVITED.value):
        raise ConflictError(
            f"Task {task.id} is {task.status}; it cannot be scheduled",
            details={"task_status": task.status},
        )
    if task.latest_start_at <= now:
        raise ConflictError(
            f"Task {task.id} start window has passed",
            details={"latest_start_at": task.latest_start_at.isoformat()},
        )
    missing = unmet_prerequisites(db, task)
    if missing:
        return SchedulingResult(
            task_id=task.id,
            status="unsatisfiable",
            violations=[
                {
                    "code": "prerequisite_not_completed",
                    "message": "A prerequisite task is not completed",
                    "context": {"required_task_ids": missing},
                }
            ],
        )

    evaluations = evaluate_task_candidates(db, task)
    ranked = constraints.rank_candidates(evaluations)
    if not ranked:
        _log(
            db,
            event_type=EventType.INVITATION_SENT,
            task_id=task.id,
            coordinator_id=coordinator_id,
            detail={
                "result": "unsatisfiable",
                "evaluations": [
                    {
                        "worker_id": e.worker_id,
                        "violations": e.violation_dicts(),
                    }
                    for e in evaluations
                ],
            },
        )
        return SchedulingResult(
            task_id=task.id,
            status="unsatisfiable",
            violations=[
                v for e in evaluations for v in e.violation_dicts()
            ],
        )

    chosen = ranked[0]
    ttl = ttl_minutes if ttl_minutes is not None else settings.invitation_ttl_minutes
    asm = _create_invitation(
        db, task, chosen.worker_id, now, timedelta(minutes=ttl), invited_by
    )
    task.status = TaskStatus.INVITED.value
    _log(
        db,
        event_type=EventType.INVITATION_SENT,
        task_id=task.id,
        assignment_id=asm.id,
        worker_id=chosen.worker_id,
        coordinator_id=coordinator_id,
        detail={
            "ranked_candidates": [
                {
                    "worker_id": e.worker_id,
                    "min_remaining_minutes": e.min_remaining_minutes,
                    "total_load_minutes": e.total_load_minutes,
                }
                for e in ranked
            ]
        },
    )
    db.flush()
    return SchedulingResult(
        task_id=task.id,
        status="invited",
        assignment_id=asm.id,
        worker_id=chosen.worker_id,
    )


def _create_invitation(
    db: Session, task: Task, worker_id: int, now: datetime, ttl: timedelta,
    invited_by: str,
) -> Assignment:
    asm = Assignment(
        task_id=task.id,
        worker_id=worker_id,
        status=AssignmentStatus.PENDING.value,
        invited_at=now,
        expires_at=now + ttl,
        task_starts_at=task.starts_at,
        task_ends_at=task.ends_at,
        invited_by=invited_by,
    )
    db.add(asm)
    db.flush()
    return asm


def _live_assignment(db: Session, task_id: int) -> Assignment | None:
    return db.scalar(
        select(Assignment)
        .where(Assignment.task_id == task_id)
        .where(
            Assignment.status.in_(
                [AssignmentStatus.PENDING.value, AssignmentStatus.ACCEPTED.value]
            )
        )
        .order_by(Assignment.id)
    )


# ---------------------------------------------------------------------------
# Accept / decline
# ---------------------------------------------------------------------------

def accept_invitation(
    db: Session,
    clock: Clock,
    assignment_id: int,
    *,
    worker_external_id: str | None = None,
) -> Assignment:
    """Accept an invitation. Idempotent and race-safe.

    * Accepting your own already-accepted invitation returns it unchanged.
    * Any other state (expired, another worker accepted, decline/cancel) ->
      409. Accepting after the 8-minute deadline is exactly the timeout race.
    """
    now = clock.now()
    db.info["now"] = now
    asm = db.get(Assignment, assignment_id)
    if asm is None:
        raise NotFoundError(f"Assignment {assignment_id} not found")
    task = _lock_task(db, asm.task_id)

    if worker_external_id is not None:
        worker = db.get(Worker, asm.worker_id)
        if worker is None or worker.external_id != worker_external_id:
            raise ConflictError(
                "This invitation belongs to another worker",
                details={"assignment_id": assignment_id},
            )

    if asm.status == AssignmentStatus.ACCEPTED.value:
        return asm  # idempotent repeat accept
    if asm.status != AssignmentStatus.PENDING.value:
        raise ConflictError(
            f"Invitation is {asm.status.value}; it cannot be accepted",
            details={"assignment_status": asm.status.value},
        )
    if asm.expires_at <= now:
        # Timeout raced the accept: expire, release the task, refuse accept.
        asm.status = AssignmentStatus.EXPIRED.value
        asm.responded_at = now
        if task.status == TaskStatus.INVITED.value:
            task.status = TaskStatus.PENDING.value
        _log(
            db,
            event_type=EventType.INVITATION_EXPIRED,
            task_id=task.id,
            assignment_id=asm.id,
            worker_id=asm.worker_id,
            detail="Expired before acceptance",
        )
        db.flush()
        raise ConflictError(
            "Invitation expired before acceptance",
            details={"expires_at": asm.expires_at.isoformat()},
        )

    # Re-validate hard constraints at accept time (another booking may have
    # landed after the invitation was sent).
    required_qids = [tq.qualification_id for tq in task.qualifications]
    worker = db.get(Worker, asm.worker_id)
    evaluation = constraints.evaluate_candidate(
        db,
        worker=worker,
        task=task,
        required_qualification_ids=required_qids,
        exclude_assignment_id=asm.id,
    )
    if not evaluation.feasible:
        # The booking is no longer safe; void the invitation and report.
        asm.status = AssignmentStatus.EXPIRED.value
        asm.responded_at = now
        if task.status == TaskStatus.INVITED.value:
            task.status = TaskStatus.PENDING.value
        _log(
            db,
            event_type=EventType.INVITATION_EXPIRED,
            task_id=task.id,
            assignment_id=asm.id,
            worker_id=asm.worker_id,
            detail={"reason": "constraints_changed", "violations": evaluation.violation_dicts()},
        )
        db.flush()
        raise ConflictError(
            "Constraints no longer satisfied at accept time",
            details={"violations": evaluation.violation_dicts()},
        )

    asm.status = AssignmentStatus.ACCEPTED.value
    asm.responded_at = now
    task.status = TaskStatus.ASSIGNED.value
    _log(
        db,
        event_type=EventType.INVITATION_ACCEPTED,
        task_id=task.id,
        assignment_id=asm.id,
        worker_id=asm.worker_id,
        detail=f"Accepted at {now.isoformat()}",
    )
    db.flush()
    return asm


def decline_invitation(
    db: Session, clock: Clock, assignment_id: int, *,
    worker_external_id: str | None = None,
) -> Assignment:
    now = clock.now()
    db.info["now"] = now
    asm = db.get(Assignment, assignment_id)
    if asm is None:
        raise NotFoundError(f"Assignment {assignment_id} not found")
    task = _lock_task(db, asm.task_id)
    if worker_external_id is not None:
        worker = db.get(Worker, asm.worker_id)
        if worker is None or worker.external_id != worker_external_id:
            raise ConflictError("This invitation belongs to another worker")
    if asm.status == AssignmentStatus.ACCEPTED.value:
        raise ConflictError("Accepted assignments cannot be declined; ask a coordinator")
    if asm.status != AssignmentStatus.PENDING.value:
        return asm
    asm.status = AssignmentStatus.DECLINED.value
    asm.responded_at = now
    task.status = TaskStatus.PENDING.value
    _log(
        db,
        event_type=EventType.INVITATION_DECLINED,
        task_id=task.id,
        assignment_id=asm.id,
        worker_id=asm.worker_id,
    )
    db.flush()
    return asm


# ---------------------------------------------------------------------------
# Timeout-aware auto re-allocation
# ---------------------------------------------------------------------------

def sweep_and_reallocate(
    db: Session, clock: Clock, *, task_ids: list[int] | None = None
) -> list[SchedulingResult]:
    """Expire due invitations and immediately invite the next candidate.

    Used by both the background scheduler (all tasks) and API endpoints that
    want an immediate re-allocation after a timeout.
    """
    expired = expire_due_invitations(db, clock)
    targets = {a.task_id for a in expired}
    if task_ids is not None:
        targets.update(task_ids)
    results: list[SchedulingResult] = []
    for task_id in sorted(targets):
        results.append(schedule_task(db, clock, task_id))
    return results


# ---------------------------------------------------------------------------
# Coordinator manual actions (same constraint engine)
# ---------------------------------------------------------------------------

def manual_assign(
    db: Session,
    clock: Clock,
    *,
    task_id: int,
    worker_id: int,
    coordinator_id: int,
    reason: str,
) -> SchedulingResult:
    """Force-invite a specific worker chosen by a coordinator.

    The worker still has to satisfy every hard constraint. Any live
    invitation is cancelled first so the manual choice is the only one.
    """
    now = clock.now()
    db.info["now"] = now
    task = _lock_task(db, task_id)
    worker = db.get(Worker, worker_id)
    if worker is None:
        raise NotFoundError(f"Worker {worker_id} not found")

    required_qids = [tq.qualification_id for tq in task.qualifications]
    evaluation = constraints.evaluate_candidate(
        db,
        worker=worker,
        task=task,
        required_qualification_ids=required_qids,
    )
    if not evaluation.feasible:
        _log(
            db,
            event_type=EventType.MANUAL_ASSIGN,
            task_id=task.id,
            worker_id=worker_id,
            coordinator_id=coordinator_id,
            detail={"reason": reason, "rejected": evaluation.violation_dicts()},
        )
        return SchedulingResult(
            task_id=task.id,
            status="unsatisfiable",
            worker_id=worker_id,
            violations=evaluation.violation_dicts(),
        )

    _cancel_live(db, task, coordinator_id, reason, now)
    ttl = settings.invitation_ttl_minutes
    asm = _create_invitation(
        db, task, worker_id, now, timedelta(minutes=ttl),
        invited_by=f"coordinator:{coordinator_id}",
    )
    task.status = TaskStatus.INVITED.value
    _log(
        db,
        event_type=EventType.MANUAL_ASSIGN,
        task_id=task.id,
        assignment_id=asm.id,
        worker_id=worker_id,
        coordinator_id=coordinator_id,
        detail={"reason": reason},
    )
    db.flush()
    return SchedulingResult(
        task_id=task.id,
        status="invited",
        assignment_id=asm.id,
        worker_id=worker_id,
    )


def manual_unassign(
    db: Session, clock: Clock, *, task_id: int, coordinator_id: int, reason: str
) -> None:
    now = clock.now()
    db.info["now"] = now
    task = _lock_task(db, task_id)
    _cancel_live(db, task, coordinator_id, reason, now)
    task.status = TaskStatus.PENDING.value
    _log(
        db,
        event_type=EventType.MANUAL_UNASSIGN,
        task_id=task.id,
        coordinator_id=coordinator_id,
        detail={"reason": reason},
    )
    db.flush()


def manual_reschedule(
    db: Session,
    clock: Clock,
    *,
    task_id: int,
    starts_at: datetime,
    duration_minutes: int,
    coordinator_id: int,
    reason: str,
) -> Task:
    """Move a task to a new interval, then re-check the existing assignee.

    If the current worker no longer fits, the live assignment is cancelled and
    the task returns to PENDING (coordinator can re-invite). Every check goes
    through the same constraint engine as automatic scheduling.
    """
    now = clock.now()
    db.info["now"] = now
    task = _lock_task(db, task_id)
    starts_at = time_utils.ensure_utc(starts_at)
    ends_at = starts_at + timedelta(minutes=duration_minutes)
    if duration_minutes <= 0:
        raise ValidationError("duration_minutes must be positive")

    live = _live_assignment(db, task.id)
    if live is not None and live.status == AssignmentStatus.ACCEPTED.value:
        required_qids = [tq.qualification_id for tq in task.qualifications]
        worker = db.get(Worker, live.worker_id)
        evaluation = constraints.evaluate_candidate(
            db,
            worker=worker,
            task=task,
            required_qualification_ids=required_qids,
            starts_at=starts_at,
            ends_at=ends_at,
            exclude_assignment_id=live.id,
        )
        if not evaluation.feasible:
            _cancel_live(db, task, coordinator_id, reason, now)

    task.starts_at = starts_at
    task.ends_at = ends_at
    task.latest_start_at = ends_at
    task.duration_minutes = duration_minutes
    task.manually_adjusted = 1
    live = _live_assignment(db, task.id)
    if live is not None:
        live.task_starts_at = starts_at
        live.task_ends_at = ends_at
        if task.status == TaskStatus.PENDING.value:
            task.status = TaskStatus.INVITED.value
    _log(
        db,
        event_type=EventType.MANUAL_RESCHEDULE,
        task_id=task.id,
        assignment_id=live.id if live else None,
        coordinator_id=coordinator_id,
        detail={
            "reason": reason,
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
        },
    )
    db.flush()
    return task


def _cancel_live(db: Session, task: Task, coordinator_id: int | None,
                 reason: str, now: datetime) -> None:
    live = _live_assignment(db, task.id)
    if live is None:
        return
    live.status = AssignmentStatus.CANCELLED.value
    live.responded_at = now
    _log(
        db,
        event_type=EventType.ASSIGNMENT_CANCELLED,
        task_id=task.id,
        assignment_id=live.id,
        worker_id=live.worker_id,
        coordinator_id=coordinator_id,
        detail={"reason": reason},
    )
