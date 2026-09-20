"""Enrolment, seat holding, waitlist promotion and 48h expiry.

Concurrency model
-----------------
Every mutation that can move a seat locks the parent ``Program`` row with
``SELECT ... FOR UPDATE`` first.  Capacity counting, expiry and waitlist
promotion therefore serialise per program: two learners racing for the last
seat cannot both win.  A partial unique index on
(program_id, learner_id) WHERE status IN ('pending','confirmed','waitlisted')
makes duplicate enrolment idempotent even if two requests somehow pass the
check side by side.

Expiry
------
A seat offered (including via waitlist promotion) must be confirmed within
``SEAT_HOLD_HOURS`` (48h by default).  Expiry is evaluated lazily against the
controllable clock before every mutation and actively by ``sweep_expired``
(the /admin/expire-seats endpoint).  Each promotion starts its *own* 48h clock.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import now_utc
from app.config import settings
from app.errors import ConflictError, ForbiddenError, NotFoundError
from app.models import Enrollment, Program, ProgramVersion
from app.statuses import (
    ACTIVE_ENROLLMENT_STATES,
    ENR_CANCELLED,
    ENR_CONFIRMED,
    ENR_EXPIRED,
    ENR_PENDING,
    ENR_WAITLISTED,
    SEAT_HOLDING_STATES,
    VERSION_PUBLISHED,
)


# ---------- helpers ----------

def _lock_program(session: Session, program_id: int) -> Program:
    program = session.execute(
        select(Program).where(Program.id == program_id).with_for_update()
    ).scalar_one_or_none()
    if program is None:
        raise NotFoundError(f"program {program_id} not found")
    return program


def _seat_hold_deadline(seat_granted_at) -> timedelta:
    return seat_granted_at + timedelta(hours=settings.SEAT_HOLD_HOURS)


def _expire_due_pending(session: Session, program: Program) -> list[Enrollment]:
    """Expire PENDING seats whose 48h hold has elapsed. Returns expired rows."""
    now = now_utc()
    pending = session.scalars(
        select(Enrollment)
        .where(
            Enrollment.program_id == program.id,
            Enrollment.status == ENR_PENDING,
        )
        .with_for_update()
    ).all()
    expired = []
    for enrollment in pending:
        if enrollment.seat_granted_at is not None and now >= _seat_hold_deadline(
            enrollment.seat_granted_at
        ):
            enrollment.status = ENR_EXPIRED
            enrollment.status_changed_at = now
            expired.append(enrollment)
    # Flush so callers that re-SELECT rows observe the new status rather than
    # the pre-update database value.
    if expired:
        session.flush()
    return expired


def _seats_used(session: Session, program: Program) -> int:
    rows = session.scalars(
        select(Enrollment).where(
            Enrollment.program_id == program.id,
            Enrollment.status.in_(SEAT_HOLDING_STATES),
        )
    ).all()
    return len(rows)


def _next_waitlist_position(session: Session, program: Program) -> int:
    highest = session.scalar(
        select(Enrollment.waitlist_position)
        .where(
            Enrollment.program_id == program.id,
            Enrollment.status == ENR_WAITLISTED,
        )
        .order_by(Enrollment.waitlist_position.desc())
        .limit(1)
    )
    return (highest or 0) + 1


def _promote_waitlist(session: Session, program: Program) -> list[Enrollment]:
    """Fill free seats from the FIFO waitlist. Each offer gets a fresh clock."""
    promoted = []
    while True:
        if _seats_used(session, program) >= program.capacity:
            break
        candidate = session.scalars(
            select(Enrollment)
            .where(
                Enrollment.program_id == program.id,
                Enrollment.status == ENR_WAITLISTED,
            )
            .order_by(Enrollment.waitlist_position.asc(), Enrollment.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()
        if candidate is None:
            break
        now = now_utc()
        candidate.status = ENR_PENDING
        candidate.waitlist_position = None
        candidate.seat_granted_at = now
        candidate.status_changed_at = now
        promoted.append(candidate)
    return promoted


def _get_enrollment_or_404(session: Session, enrollment_id: int) -> Enrollment:
    enrollment = session.get(Enrollment, enrollment_id)
    if enrollment is None:
        raise NotFoundError(f"enrollment {enrollment_id} not found")
    return enrollment


def _require_owner(enrollment: Enrollment, learner_id: int) -> None:
    if enrollment.learner_id != learner_id:
        raise ForbiddenError("learners may only act on their own enrollment")


# ---------- operations ----------

def enroll(session: Session, program_id: int, learner_id: int) -> Enrollment:
    program = _lock_program(session, program_id)

    # Fast idempotency path, re-checked under the lock below.
    existing = _active_enrollment(session, program_id, learner_id)
    if existing is not None:
        return existing

    version = session.get(ProgramVersion, program.current_version_id) if (
        program.current_version_id
    ) else None
    if version is None or version.status != VERSION_PUBLISHED:
        raise ConflictError("program has no published version to enrol in")
    if now_utc() > program.enrollment_deadline:
        raise ConflictError("enrollment deadline has passed")

    # Serialised section: expire stale holds, then decide seat vs waitlist.
    _expire_due_pending(session, program)
    existing = _active_enrollment(session, program_id, learner_id)
    if existing is not None:
        return existing

    now = now_utc()
    if _seats_used(session, program) < program.capacity:
        enrollment = Enrollment(
            program_id=program_id,
            version_id=version.id,  # pinned: later publishes never touch this
            learner_id=learner_id,
            status=ENR_PENDING,
            seat_granted_at=now,
        )
    else:
        enrollment = Enrollment(
            program_id=program_id,
            version_id=version.id,
            learner_id=learner_id,
            status=ENR_WAITLISTED,
            waitlist_position=_next_waitlist_position(session, program),
        )
    session.add(enrollment)
    session.flush()
    # Promotion cannot be due on a brand-new enrolment, but running the
    # promotion loop keeps the invariant "one transaction settles the queue".
    _promote_waitlist(session, program)
    session.commit()
    session.refresh(enrollment)
    return enrollment


def _active_enrollment(session: Session, program_id: int, learner_id: int) -> Enrollment | None:
    return session.scalars(
        select(Enrollment)
        .where(
            Enrollment.program_id == program_id,
            Enrollment.learner_id == learner_id,
            Enrollment.status.in_(ACTIVE_ENROLLMENT_STATES),
        )
        .order_by(Enrollment.id.desc())
    ).first()


def _settle_expiries(session: Session, program: Program) -> bool:
    """Expire overdue holds and promote waitlist. Commits if anything changed."""
    expired = _expire_due_pending(session, program)
    if not expired:
        return False
    _promote_waitlist(session, program)
    session.commit()
    return True


def confirm(session: Session, enrollment_id: int, learner_id: int) -> Enrollment:
    enrollment = _get_enrollment_or_404(session, enrollment_id)
    program = _lock_program(session, enrollment.program_id)
    _require_owner(enrollment, learner_id)

    if _settle_expiries(session, program):
        # Expiry/promotion were committed; re-read this row's new status.
        enrollment = _get_enrollment_or_404(session, enrollment_id)

    if enrollment.status == ENR_CONFIRMED:
        return enrollment  # idempotent
    if enrollment.status == ENR_EXPIRED:
        raise ConflictError("seat offer expired before confirmation")
    if enrollment.status == ENR_CANCELLED:
        raise ConflictError("enrollment was cancelled")
    if enrollment.status == ENR_WAITLISTED:
        raise ConflictError("no seat held yet; still on the waitlist")
    now = now_utc()
    enrollment.status = ENR_CONFIRMED
    enrollment.confirmed_at = now
    enrollment.status_changed_at = now
    session.commit()
    session.refresh(enrollment)
    return enrollment


def cancel(session: Session, enrollment_id: int, learner_id: int) -> Enrollment:
    enrollment = _get_enrollment_or_404(session, enrollment_id)
    program = _lock_program(session, enrollment.program_id)
    _require_owner(enrollment, learner_id)

    if _settle_expiries(session, program):
        enrollment = _get_enrollment_or_404(session, enrollment_id)

    if enrollment.status in (ENR_CANCELLED, ENR_EXPIRED):
        return enrollment  # idempotent terminal state

    held_seat = enrollment.status in (ENR_PENDING, ENR_CONFIRMED)
    now = now_utc()
    enrollment.status = ENR_CANCELLED
    enrollment.waitlist_position = None
    enrollment.status_changed_at = now
    session.flush()

    if held_seat:
        _promote_waitlist(session, program)
    session.commit()
    session.refresh(enrollment)
    return enrollment


def sweep_expired(session: Session, program_id: int | None = None) -> dict:
    """Expire overdue PENDING seats across (one|all) programs and promote.

    One transaction per program so a failure on one program does not poison
    processing of the others.
    """
    program_ids = [program_id] if program_id is not None else [
        pid for (pid,) in session.execute(select(Program.id)).all()
    ]
    total_expired = 0
    total_promoted = 0
    for pid in program_ids:
        try:
            program = _lock_program(session, pid)
        except NotFoundError:
            continue
        expired = _expire_due_pending(session, program)
        promoted = _promote_waitlist(session, program) if expired else []
        session.commit()
        total_expired += len(expired)
        total_promoted += len(promoted)
    return {"expired": total_expired, "promoted": total_promoted}
