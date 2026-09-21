"""Allocation, invitation acceptance and expiry/reallocation.

Concurrency model (PostgreSQL)
------------------------------
* Both allocation/acceptance and the expiry reaper acquire the **task row
  lock first** (``SELECT tasks ... FOR UPDATE``) and then lock that task's
  invitation rows. Consistent lock ordering task -> invitations eliminates
  deadlocks between "accept racing expiry" and "expire racing accept".
* A task can hold at most one assignment row (unique constraint), so even
  under concurrent accept requests only one worker can land on the roster;
  the loser gets a clean conflict response and never consumes capacity.
* Constraints are re-checked under the row lock at accept time, so an
  invitation accepted after a manual change cannot bypass the rules.
* Invitation rows are always read with explicit locking SELECTs, never via a
  possibly stale cached ORM relationship collection.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    Assignment,
    AssignmentStatus,
    Coordinator,
    Invitation,
    InvitationStatus,
    Task,
    TaskStatus,
    Worker,
)
from app.services import audit
from app.services.constraints import (
    C_TASK_NOT_OPEN,
    RankedCandidate,
    Violation,
    evaluate_candidate,
)
from app.services.allocation_candidates import choose_candidate
from app.services.timeutils import as_utc

OPEN_FOR_ALLOCATION = {TaskStatus.PLANNED.value, TaskStatus.INVITED.value, TaskStatus.UNASSIGNED.value}
_TERMINAL_INVITATION = (
    InvitationStatus.EXPIRED.value,
    InvitationStatus.CANCELLED.value,
    InvitationStatus.DECLINED.value,
    InvitationStatus.ACCEPTED.value,
)


class AllocationError(Exception):
    def __init__(self, message: str, violations: list[Violation] | None = None, code: str = "allocation_error"):
        super().__init__(message)
        self.violations = violations or []
        self.code = code


@dataclass
class AllocationResult:
    task_id: int
    status: str                    # invited | assigned | unassigned | unchanged
    worker_id: int | None
    invitation_id: int | None
    violations: list[dict]         # per-worker unsatisfied constraints when nobody fits
    round: int | None


def lock_task(session: Session, task_id: int) -> Task:
    task = session.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None:
        raise AllocationError(f"task {task_id} not found", code="not_found")
    return task


def _lock_invitations(session: Session, task_id: int) -> list[Invitation]:
    """Lock and return every invitation of a task (task lock must be held)."""
    return list(session.scalars(
        select(Invitation)
        .where(Invitation.task_id == task_id)
        .order_by(Invitation.id)
        .with_for_update()
    ))


def _tried_this_cycle(invitations: list[Invitation], generation: int) -> set[int]:
    """Workers who already had a non-pending invitation in this generation."""
    return {
        inv.worker_id
        for inv in invitations
        if inv.generation == generation and inv.status in _TERMINAL_INVITATION
    }


def _ever_tried(invitations: list[Invitation]) -> bool:
    return any(inv.status in _TERMINAL_INVITATION for inv in invitations)


def _expire_stale_invitations(invitations: list[Invitation], now: datetime) -> int:
    """Mark due pending invitations expired. Returns how many expired."""
    count = 0
    for inv in invitations:
        if inv.status == InvitationStatus.PENDING.value and as_utc(inv.expires_at) <= now:
            inv.status = InvitationStatus.EXPIRED.value
            inv.responded_at = now
            count += 1
    return count


def _violations_payload(ranked: list[RankedCandidate]) -> list[dict]:
    return [
        {
            "worker_id": rc.worker.id,
            "constraints": [
                {"constraint": v.constraint, "message": v.message,
                 **({"detail": v.detail} if v.detail else {})}
                for v in rc.violations
            ],
        }
        for rc in ranked
    ]


def allocate_task(
    session: Session,
    task_id: int,
    *,
    now: datetime,
    settings: Settings,
    actor_id: str = "system",
) -> AllocationResult:
    """Invite the best feasible candidate for one task.

    Expired invitations are swept first; workers who already had a turn in
    this search generation are skipped. When nobody fits, the task becomes
    ``unassigned`` and the concrete unsatisfied constraints are returned.
    """
    now = as_utc(now)
    task = lock_task(session, task_id)
    invitations = _lock_invitations(session, task_id)
    expired = _expire_stale_invitations(invitations, now)

    if task.status in (
        TaskStatus.ASSIGNED.value,
        TaskStatus.IN_PROGRESS.value,
        TaskStatus.COMPLETED.value,
        TaskStatus.CANCELLED.value,
    ):
        return AllocationResult(task.id, "unchanged", None, None, [], None)

    if task.status not in OPEN_FOR_ALLOCATION:
        raise AllocationError(
            f"task {task.id} status {task.status} cannot be allocated",
            violations=[Violation(C_TASK_NOT_OPEN, f"task status is {task.status}")],
            code="task_not_open",
        )

    # A still-live invitation window blocks creating another one.
    pending = [i for i in invitations if i.status == InvitationStatus.PENDING.value]
    if pending and expired == 0:
        return AllocationResult(task.id, "unchanged", None, None, [], None)

    tried = _tried_this_cycle(invitations, task.allocation_generation)
    ranked = choose_candidate(session, task, exclude=tried)
    feasible = [rc for rc in ranked if rc.feasible]

    current_round = max((i.round for i in invitations), default=0) + 1
    if not feasible and tried:
        # Whole generation exhausted without an acceptance: open a new
        # generation and re-rank everyone. A lone feasible worker is therefore
        # re-invited after each timeout (timeout -> reassign); the task only
        # becomes unassigned if nobody is feasible at all.
        task.allocation_generation += 1
        ranked = choose_candidate(session, task, exclude=set())
        feasible = [rc for rc in ranked if rc.feasible]
        current_round = max((i.round for i in invitations), default=0) + 1

    if not feasible:
        task.status = TaskStatus.UNASSIGNED.value
        # Full unfiltered ranking for the error report (includes the workers
        # who already had a turn this generation).
        full_ranked = choose_candidate(session, task, exclude=set())
        violations = _violations_payload(full_ranked)
        audit.record(
            session, action="task.unassigned", actor_type="system", actor_id=actor_id,
            task_id=task.id, reason="no feasible candidate after constraints evaluation",
            detail={"violations": violations, "generation_exhausted": _ever_tried(invitations)},
            now=now,
        )
        return AllocationResult(task.id, "unassigned", None, None, violations, None)

    best = feasible[0]
    invitation = Invitation(
        task_id=task.id,
        worker_id=best.worker.id,
        round=current_round,
        generation=task.allocation_generation,
        status=InvitationStatus.PENDING.value,
        created_at=now,
        expires_at=now + timedelta(minutes=settings.invitation_ttl_minutes),
    )
    session.add(invitation)
    task.status = TaskStatus.INVITED.value
    session.flush()
    audit.record(
        session, action="invitation.created", actor_type="system", actor_id=actor_id,
        task_id=task.id,
        reason=f"round {current_round} generation {task.allocation_generation}: worker {best.worker.id} ranks best",
        detail={
            "invitation_id": invitation.id,
            "worker_id": best.worker.id,
            "round": current_round,
            "generation": task.allocation_generation,
            "rank": {
                "min_remaining_minutes": best.min_remaining_minutes,
                "existing_load_minutes": best.existing_load_minutes,
            },
        },
        now=now,
    )
    return AllocationResult(task.id, "invited", best.worker.id, invitation.id, [], current_round)


def expire_due_invitations(session: Session, *, now: datetime, settings: Settings) -> list[int]:
    """Sweep every due invitation and reallocate its task.

    Called by the background reaper and exposed via an internal endpoint so
    tests can drive the clock deterministically.
    """
    now = as_utc(now)
    due_task_ids = [
        row[0]
        for row in session.execute(
            select(Invitation.task_id)
            .where(
                Invitation.status == InvitationStatus.PENDING.value,
                Invitation.expires_at <= now,
            )
            .distinct()
        ).all()
    ]
    affected: list[int] = []
    for task_id in due_task_ids:
        task = lock_task(session, task_id)
        invitations = _lock_invitations(session, task_id)
        expired = _expire_stale_invitations(invitations, now)
        if not expired:
            continue
        audit.record(
            session, action="invitation.expired", actor_type="system", actor_id="reaper",
            task_id=task.id,
            reason=f"{expired} invitation(s) passed {settings.invitation_ttl_minutes} min TTL",
            detail={"expired": expired}, now=now,
        )
        # Reallocate immediately so the next candidate gets the window.
        if task.status == TaskStatus.INVITED.value:
            allocate_task(session, task.id, now=now, settings=settings, actor_id="reaper")
        affected.append(task.id)
    return affected


def accept_invitation(
    session: Session,
    invitation_id: int,
    worker_id: int,
    *,
    now: datetime,
) -> Assignment:
    """Worker accepts an invitation.

    Idempotent: if this invitation was already accepted the existing
    assignment is returned (duplicate requests never double-count hours).
    All races collapse on the task row lock + assignment unique constraint.
    """
    now = as_utc(now)
    invitation = session.get(Invitation, invitation_id)
    if invitation is None:
        raise AllocationError(f"invitation {invitation_id} not found", code="not_found")
    if invitation.worker_id != worker_id:
        raise AllocationError("invitation belongs to another worker", code="forbidden")

    # Lock the task FIRST (task -> invitation lock order, same as the reaper).
    task = lock_task(session, invitation.task_id)
    # Re-read the invitation under the lock to observe reaper/cancel decisions.
    invitations = _lock_invitations(session, task.id)
    invitation = next((i for i in invitations if i.id == invitation_id), None)
    if invitation is None:  # pragma: no cover - defensive
        raise AllocationError("invitation vanished", code="not_found")

    existing = session.scalar(select(Assignment).where(Assignment.task_id == task.id))
    if existing is not None:
        if existing.invitation_id == invitation.id:
            return existing  # idempotent repeat accept
        raise AllocationError("task already assigned to another worker", code="already_assigned")

    if as_utc(invitation.expires_at) <= now and invitation.status == InvitationStatus.PENDING.value:
        invitation.status = InvitationStatus.EXPIRED.value
        invitation.responded_at = now
        audit.record(
            session, action="invitation.expired", actor_type="system", actor_id="reaper",
            task_id=task.id, reason="expired at accept attempt",
            detail={"invitation_id": invitation.id}, now=now,
        )
        raise AllocationError("invitation expired before acceptance", code="invitation_expired")
    if invitation.status != InvitationStatus.PENDING.value:
        raise AllocationError(
            f"invitation is {invitation.status}", code=f"invitation_{invitation.status}"
        )

    worker = session.get(Worker, worker_id)
    violations = evaluate_candidate(session, worker=worker, task=task)
    if violations:
        # Constraints changed since the invitation was sent (e.g. manual change);
        # the accept is refused instead of silently breaking a rule.
        audit.record(
            session, action="invitation.accept_blocked", actor_type="worker",
            actor_id=str(worker_id), task_id=task.id,
            reason="constraints no longer satisfied at accept time",
            detail={"violations": [v.__dict__ for v in violations]}, now=now,
        )
        raise AllocationError("constraints no longer satisfied", violations=violations,
                              code="constraint_violation")

    assignment = Assignment(
        task_id=task.id,
        worker_id=worker_id,
        invitation_id=invitation.id,
        status=AssignmentStatus.ASSIGNED.value,
        assigned_at=now,
    )
    session.add(assignment)
    try:
        session.flush()
    except IntegrityError as e:
        # Another concurrent accept won the unique-constraint race.
        session.rollback()
        raise AllocationError("task already assigned to another worker", code="already_assigned") from e

    invitation.status = InvitationStatus.ACCEPTED.value
    invitation.responded_at = now
    # All sibling pending invitations die immediately — only one valid allocation survives.
    for other in invitations:
        if other.id != invitation.id and other.status == InvitationStatus.PENDING.value:
            other.status = InvitationStatus.CANCELLED.value
            other.responded_at = now

    task.status = TaskStatus.ASSIGNED.value
    audit.record(
        session, action="assignment.created", actor_type="worker", actor_id=str(worker_id),
        task_id=task.id, reason="worker accepted invitation",
        detail={"invitation_id": invitation.id, "worker_id": worker_id}, now=now,
    )
    return assignment


def decline_invitation(session: Session, invitation_id: int, worker_id: int, *, now: datetime,
                       settings: Settings) -> None:
    now = as_utc(now)
    invitation = session.get(Invitation, invitation_id)
    if invitation is None:
        raise AllocationError("invitation not found", code="not_found")
    if invitation.worker_id != worker_id:
        raise AllocationError("invitation belongs to another worker", code="forbidden")
    task = lock_task(session, invitation.task_id)
    invitations = _lock_invitations(session, task.id)
    invitation = next(i for i in invitations if i.id == invitation_id)
    if invitation.status != InvitationStatus.PENDING.value:
        raise AllocationError(f"invitation is {invitation.status}", code=f"invitation_{invitation.status}")
    invitation.status = InvitationStatus.DECLINED.value
    invitation.responded_at = now
    audit.record(
        session, action="invitation.declined", actor_type="worker", actor_id=str(worker_id),
        task_id=task.id, reason="worker declined", detail={"invitation_id": invitation.id}, now=now,
    )
    allocate_task(session, task.id, now=now, settings=settings, actor_id="reaper")


def manual_assign(
    session: Session,
    task_id: int,
    worker_id: int,
    *,
    now: datetime,
    coordinator: Coordinator,
    reason: str,
) -> Assignment:
    """Coordinator forces an assignment — same constraint checks, with a recorded reason."""
    now = as_utc(now)
    task = lock_task(session, task_id)
    invitations = _lock_invitations(session, task_id)
    if task.status in (TaskStatus.CANCELLED.value, TaskStatus.COMPLETED.value,
                       TaskStatus.IN_PROGRESS.value):
        raise AllocationError(f"task {task.status} cannot be manually assigned",
                              code="task_not_open")
    worker = session.get(Worker, worker_id)
    if worker is None:
        raise AllocationError(f"worker {worker_id} not found", code="not_found")

    violations = evaluate_candidate(session, worker=worker, task=task)
    if violations:
        raise AllocationError("candidate does not satisfy scheduling constraints",
                              violations=violations, code="constraint_violation")

    existing = session.scalar(select(Assignment).where(Assignment.task_id == task.id))
    if existing is not None:
        audit.record(
            session, action="assignment.manual_reassigned", actor_type="coordinator",
            actor_id=str(coordinator.id), task_id=task.id, reason=reason,
            detail={"from_worker_id": existing.worker_id, "to_worker_id": worker_id,
                    "released_invitation_id": existing.invitation_id},
            now=now,
        )
        session.delete(existing)
        session.flush()

    for inv in invitations:
        if inv.status in (InvitationStatus.PENDING.value, InvitationStatus.ACCEPTED.value):
            inv.status = InvitationStatus.CANCELLED.value
            inv.responded_at = now

    invitation = Invitation(
        task_id=task.id, worker_id=worker_id,
        round=max((i.round for i in invitations), default=0) + 1,
        generation=task.allocation_generation,
        status=InvitationStatus.ACCEPTED.value,
        created_at=now, expires_at=now, responded_at=now,
    )
    session.add(invitation)
    session.flush()
    assignment = Assignment(
        task_id=task.id, worker_id=worker_id, invitation_id=invitation.id,
        status=AssignmentStatus.ASSIGNED.value, assigned_at=now,
    )
    session.add(assignment)
    try:
        session.flush()
    except IntegrityError as e:
        session.rollback()
        raise AllocationError("task already assigned (concurrent change)",
                              code="already_assigned") from e

    task.status = TaskStatus.ASSIGNED.value
    audit.record(
        session, action="assignment.manual_created", actor_type="coordinator",
        actor_id=str(coordinator.id), task_id=task.id, reason=reason,
        detail={"worker_id": worker_id, "invitation_id": invitation.id}, now=now,
    )
    return assignment


def manual_unassign(
    session: Session, task_id: int, *, now: datetime, coordinator: Coordinator, reason: str
) -> None:
    now = as_utc(now)
    task = lock_task(session, task_id)
    invitations = _lock_invitations(session, task_id)
    assignment = session.scalar(select(Assignment).where(Assignment.task_id == task.id))
    if assignment is None:
        raise AllocationError("task has no assignment", code="not_assigned")
    audit.record(
        session, action="assignment.manual_released", actor_type="coordinator",
        actor_id=str(coordinator.id), task_id=task.id, reason=reason,
        detail={"worker_id": assignment.worker_id, "invitation_id": assignment.invitation_id},
        now=now,
    )
    session.delete(assignment)
    inv = next((i for i in invitations if i.id == assignment.invitation_id), None)
    if inv is not None and inv.status == InvitationStatus.ACCEPTED.value:
        inv.status = InvitationStatus.CANCELLED.value
        inv.responded_at = now
    task.status = TaskStatus.PLANNED.value


def manual_reschedule(
    session: Session,
    task_id: int,
    *,
    new_start: datetime,
    new_end: datetime,
    now: datetime,
    coordinator: Coordinator,
    reason: str,
) -> Task:
    """Coordinator moves a task to a new interval. Constraint-checked like any change."""
    now = as_utc(now)
    new_start, new_end = as_utc(new_start), as_utc(new_end)
    if new_end <= new_start:
        raise AllocationError("new interval must have end > start", code="bad_interval")
    task = lock_task(session, task_id)
    if task.status == TaskStatus.CANCELLED.value:
        raise AllocationError("cannot reschedule a cancelled task", code="task_not_open")

    before = {
        "scheduled_start": as_utc(task.scheduled_start).isoformat(),
        "scheduled_end": as_utc(task.scheduled_end).isoformat(),
    }
    task.scheduled_start, task.scheduled_end = new_start, new_end

    assignment = session.scalar(select(Assignment).where(Assignment.task_id == task.id))
    violations: list[Violation] = []
    if assignment is not None:
        worker = session.get(Worker, assignment.worker_id)
        violations = evaluate_candidate(session, worker=worker, task=task)
    if violations:
        raise AllocationError(
            "current assignee no longer satisfies constraints at the new time; "
            "unassign or pick another interval",
            violations=violations, code="constraint_violation",
        )

    # A pending invitation was offered for the old interval; the invited
    # worker may no longer be feasible at the new time, so cancel it and
    # return the task to the allocation pool (acceptance re-checks too, this
    # just makes the state explicit and auditable).
    cancelled_pending: list[int] = []
    for inv in session.scalars(
        select(Invitation).where(
            Invitation.task_id == task.id,
            Invitation.status == InvitationStatus.PENDING.value,
        )
    ):
        inv.status = InvitationStatus.CANCELLED.value
        inv.responded_at = now
        cancelled_pending.append(inv.id)
    if assignment is None and cancelled_pending:
        task.status = TaskStatus.PLANNED.value
        task.allocation_generation += 1

    audit.record(
        session, action="task.manual_rescheduled", actor_type="coordinator",
        actor_id=str(coordinator.id), task_id=task.id, reason=reason,
        detail={"before": before,
                "after": {"scheduled_start": new_start.isoformat(),
                          "scheduled_end": new_end.isoformat()},
                "cancelled_pending_invitation_ids": cancelled_pending},
        now=now,
    )
    return task


def start_task(session: Session, task_id: int, *, now: datetime, actor_id: str) -> Task:
    now = as_utc(now)
    task = lock_task(session, task_id)
    if task.status != TaskStatus.ASSIGNED.value:
        raise AllocationError(f"only assigned tasks can start (is {task.status})",
                              code="task_not_open")
    assignment = session.scalar(select(Assignment).where(Assignment.task_id == task.id))
    if assignment is None or str(assignment.worker_id) != str(actor_id):
        raise AllocationError("task is assigned to another worker", code="forbidden")
    task.status = TaskStatus.IN_PROGRESS.value
    assignment.status = AssignmentStatus.IN_PROGRESS.value
    assignment.started_at = now
    audit.record(session, action="task.started", actor_type="worker", actor_id=actor_id,
                 task_id=task.id, reason="worker started service", now=now)
    return task


def complete_task(session: Session, task_id: int, *, now: datetime, actor_id: str) -> Task:
    now = as_utc(now)
    task = lock_task(session, task_id)
    if task.status not in (TaskStatus.ASSIGNED.value, TaskStatus.IN_PROGRESS.value):
        raise AllocationError(f"only assigned/in_progress tasks can complete (is {task.status})",
                              code="task_not_open")
    assignment = session.scalar(select(Assignment).where(Assignment.task_id == task.id))
    if assignment is None or str(assignment.worker_id) != str(actor_id):
        raise AllocationError("task is assigned to another worker", code="forbidden")
    task.status = TaskStatus.COMPLETED.value
    assignment.status = AssignmentStatus.COMPLETED.value
    assignment.completed_at = now
    audit.record(session, action="task.completed", actor_type="worker", actor_id=actor_id,
                 task_id=task.id, reason="worker completed service", now=now)
    return task
