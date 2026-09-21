"""Courses, enrolment, waitlist, seat offer/confirmation/expiry.

Concurrency strategy (PostgreSQL):
* Every operation that changes seat accounting locks the ``courses`` row
  first (``SELECT ... FOR UPDATE``), serialising capacity decisions for a
  course. The last available seat can therefore never be taken twice.
* Enrolment rows are then locked individually (``SELECT ... FOR UPDATE``)
  so cancel / confirm / expiry acting on the same seat cannot interleave.
* The expiry sweep takes the course lock per course and re-checks every
  timed-out seat in one transaction, cascading promotions deterministically.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock as clock_svc
from app.config import get_settings
from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import (
    Course,
    Enrollment,
    ProgramVersion,
    ENROLLMENT_CANCELLED,
    ENROLLMENT_CONFIRMED,
    ENROLLMENT_EXPIRED,
    ENROLLMENT_PENDING,
    ENROLLMENT_WAITLISTED,
    VERSION_PUBLISHED,
)

settings = get_settings()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _refresh_locked(db: Session, obj) -> None:
    """Flush pending writes, then (re)acquire the row lock and reload state.

    Ordering matters: refreshing with FOR UPDATE *before* a pending flush
    would silently overwrite the caller's unflushed attribute changes with
    the old database values.
    """
    db.flush()
    db.refresh(obj, with_for_update=True)


def _lock_course(db: Session, course_id: int) -> Course:
    course = db.get(Course, course_id)
    if course is None:
        raise NotFoundError(f"Course {course_id} not found.")
    _refresh_locked(db, course)
    return course


def _locked_enrollment(
    db: Session, course_id: int, learner_id: int
) -> Enrollment | None:
    enrollment = db.scalar(
        select(Enrollment).where(
            Enrollment.course_id == course_id,
            Enrollment.learner_id == learner_id,
        )
    )
    if enrollment is not None:
        _refresh_locked(db, enrollment)
    return enrollment


def _seat_takers(db: Session, course_id: int) -> list[Enrollment]:
    """Enrolments currently consuming a seat (offered or confirmed)."""
    return list(
        db.scalars(
            select(Enrollment)
            .where(
                Enrollment.course_id == course_id,
                Enrollment.status.in_(
                    [ENROLLMENT_PENDING, ENROLLMENT_CONFIRMED]
                ),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )


def _active_waitlist(db: Session, course_id: int) -> list[Enrollment]:
    return list(
        db.scalars(
            select(Enrollment)
            .where(
                Enrollment.course_id == course_id,
                Enrollment.status == ENROLLMENT_WAITLISTED,
            )
            .order_by(Enrollment.waitlist_position, Enrollment.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )


def _next_waitlist_position(db: Session, course_id: int) -> int:
    # Position within the *current* queue. Positions are dense (1..N) so the
    # FIFO rank of a waitlisted enrolment is always a small stable number;
    # promotion order is tie-broken by id.
    current = db.scalar(
        select(func.max(Enrollment.waitlist_position))
        .where(
            Enrollment.course_id == course_id,
            Enrollment.status == ENROLLMENT_WAITLISTED,
        )
    )
    return (current or 0) + 1


def _offer_seat(db: Session, enrollment: Enrollment, now: datetime) -> None:
    enrollment.status = ENROLLMENT_PENDING
    # Leaving the queue: release the (monotonic) FIFO position so ranking
    # counts only currently-waitlisted enrolments.
    enrollment.waitlist_position = None
    enrollment.offered_at = now.replace(tzinfo=None)
    enrollment.seat_expires_at = (
        now + timedelta(hours=settings.seat_confirm_hours)
    ).replace(tzinfo=None)
    enrollment.confirmed_at = None
    enrollment.cancelled_at = None


# ---------------------------------------------------------------------------
# Courses
# ---------------------------------------------------------------------------

def create_course(
    db: Session,
    *,
    title: str,
    version_id: int,
    capacity: int,
    enroll_deadline: datetime,
) -> Course:
    if capacity < 0:
        raise ValidationError("Capacity must be >= 0.")
    version = db.get(ProgramVersion, version_id)
    if version is None:
        raise NotFoundError(f"Version {version_id} not found.")
    if version.status != VERSION_PUBLISHED:
        raise ValidationError("Courses can only be created from published versions.")
    course = Course(
        title=title,
        version_id=version_id,
        capacity=capacity,
        enroll_deadline=_aware(enroll_deadline).replace(tzinfo=None),
    )
    db.add(course)
    db.flush()
    return course


def course_view(db: Session, course_id: int) -> dict:
    course = db.get(Course, course_id)
    if course is None:
        raise NotFoundError(f"Course {course_id} not found.")
    # Read-only accounting: no row locks needed here.
    taken = db.scalar(
        select(func.count())
        .select_from(Enrollment)
        .where(
            Enrollment.course_id == course_id,
            Enrollment.status.in_([ENROLLMENT_PENDING, ENROLLMENT_CONFIRMED]),
        )
    )
    waiting = db.scalar(
        select(func.count())
        .select_from(Enrollment)
        .where(
            Enrollment.course_id == course_id,
            Enrollment.status == ENROLLMENT_WAITLISTED,
        )
    )
    return course.to_dict(
        {
            "seats_taken": taken,
            "seats_available": course.capacity - taken,
            "waitlist_count": waiting,
        }
    )


# ---------------------------------------------------------------------------
# Enrolment
# ---------------------------------------------------------------------------

def enroll(db: Session, *, course_id: int, learner_id: int) -> Enrollment:
    """Idempotent enrolment:

    * an active enrolment (waitlist/pending/confirmed) is returned unchanged;
    * a finished enrolment (cancelled/expired) may re-register — the same row
      is reused, keeping one row per (course, learner).
    """
    current = clock_svc.now(db)
    course = _lock_course(db, course_id)
    enrollment = _locked_enrollment(db, course_id, learner_id)

    if enrollment is not None:
        if enrollment.status in (
            ENROLLMENT_WAITLISTED,
            ENROLLMENT_PENDING,
            ENROLLMENT_CONFIRMED,
        ):
            return enrollment  # idempotent
        if enrollment.certificate is not None:
            raise ConflictError(
                "This enrolment already holds a certificate and cannot be reopened."
            )
        # Reuse the cancelled/expired row as a fresh application.
        if _aware(course.enroll_deadline) <= current:
            raise ConflictError("Enrolment deadline has passed.")
        target = enrollment
    else:
        if _aware(course.enroll_deadline) <= current:
            raise ConflictError("Enrolment deadline has passed.")
        target = Enrollment(
            course_id=course_id,
            learner_id=learner_id,
            version_id=course.version_id,  # pinned at application time
            status=ENROLLMENT_WAITLISTED,
        )
        db.add(target)

    taken = len(_seat_takers(db, course_id))
    if taken < course.capacity:
        _offer_seat(db, target, current)
    else:
        target.status = ENROLLMENT_WAITLISTED
        target.waitlist_position = _next_waitlist_position(db, course_id)
        target.offered_at = None
        target.seat_expires_at = None
    db.flush()
    return target


def _get_owned_enrollment(
    db: Session, course_id: int, learner_id: int, *, lock: bool = True
) -> Enrollment:
    stmt = select(Enrollment).where(
        Enrollment.course_id == course_id,
        Enrollment.learner_id == learner_id,
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    enrollment = db.scalar(stmt)
    if enrollment is None:
        raise NotFoundError("No enrolment found for this learner in the course.")
    return enrollment


def confirm(db: Session, *, course_id: int, learner_id: int) -> Enrollment:
    current = clock_svc.now(db)
    _lock_course(db, course_id)
    enrollment = _get_owned_enrollment(db, course_id, learner_id)

    if enrollment.status == ENROLLMENT_CONFIRMED:
        return enrollment  # idempotent
    if enrollment.status == ENROLLMENT_WAITLISTED:
        raise ConflictError("You are on the waitlist; no seat has been offered yet.")
    if enrollment.status in (ENROLLMENT_CANCELLED, ENROLLMENT_EXPIRED):
        raise ConflictError(f"Enrolment is {enrollment.status}; please re-register.")
    # pending_confirmation
    if enrollment.seat_expires_at and _aware(enrollment.seat_expires_at) <= current:
        # Should normally have been swept; expire inline and promote next.
        enrollment.status = ENROLLMENT_EXPIRED
        enrollment.offered_at = None
        enrollment.seat_expires_at = None
        db.flush()
        _promote_waitlist(db, course_id, current)
        db.flush()
        raise ConflictError("The offered seat has expired; you have been moved back.")
    enrollment.status = ENROLLMENT_CONFIRMED
    enrollment.confirmed_at = current.replace(tzinfo=None)
    enrollment.seat_expires_at = None
    db.flush()
    return enrollment


def cancel(db: Session, *, course_id: int, learner_id: int) -> Enrollment:
    current = clock_svc.now(db)
    _lock_course(db, course_id)
    enrollment = _get_owned_enrollment(db, course_id, learner_id)

    if enrollment.status == ENROLLMENT_CANCELLED:
        return enrollment  # idempotent
    if enrollment.status == ENROLLMENT_CONFIRMED and enrollment.completed_at:
        raise ConflictError("A completed enrolment cannot be cancelled.")

    was_holding = enrollment.status in (ENROLLMENT_PENDING, ENROLLMENT_CONFIRMED)
    enrollment.status = ENROLLMENT_CANCELLED
    enrollment.cancelled_at = current.replace(tzinfo=None)
    enrollment.waitlist_position = None
    enrollment.offered_at = None
    enrollment.seat_expires_at = None
    enrollment.confirmed_at = None

    if was_holding:
        # Flush the released seat BEFORE recounting, then promote within
        # the same locked transaction.
        db.flush()
        _promote_waitlist(db, course_id, current)
    db.flush()
    return enrollment


def _promote_waitlist(db: Session, course_id: int, current: datetime) -> list[int]:
    """Promote eligible waitlisted learners in queue order into freed seats.

    Must be called while the course row is locked. Eligibility re-checks the
    enrolment deadline: applications after the deadline never existed, and
    waitlisted applicants retain their FIFO position regardless of deadline.
    """
    course = db.get(Course, course_id)
    promoted: list[int] = []
    waiting = _active_waitlist(db, course_id)
    if not waiting:
        return promoted

    free = course.capacity - len(_seat_takers(db, course_id))
    promoted: list[int] = []
    remaining: list[Enrollment] = []
    for enrollment in waiting:
        if free > 0:
            _offer_seat(db, enrollment, current)
            promoted.append(enrollment.id)
            free -= 1
        else:
            remaining.append(enrollment)
    # Compact the remaining queue into dense 1..N positions (FIFO preserved).
    for rank, enrollment in enumerate(remaining, start=1):
        enrollment.waitlist_position = rank
    if promoted:
        db.flush()
    return promoted


# ---------------------------------------------------------------------------
# Expiry sweep
# ---------------------------------------------------------------------------

def sweep_expired_seats(db: Session, *, course_id: int | None = None) -> dict:
    """Expire unconfirmed offers older than 48h and promote the waitlist.

    Courses are processed one at a time, each under the course row lock,
    within the caller's transaction so cancel/confirm/expiry for a given
    course always observe a single consistent state.
    """
    current = clock_svc.now(db)

    course_ids = list(
        db.scalars(
            select(Course.id).order_by(Course.id)
            if course_id is None
            else select(Course.id).where(Course.id == course_id)
        ).all()
    )

    expired_ids: list[int] = []
    promoted_ids: list[int] = []
    for cid in course_ids:
        _lock_course(db, cid)
        pending = list(
            db.scalars(
                select(Enrollment)
                .where(
                    Enrollment.course_id == cid,
                    Enrollment.status == ENROLLMENT_PENDING,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
        )
        changed = False
        for enrollment in pending:
            if (
                enrollment.seat_expires_at
                and _aware(enrollment.seat_expires_at) <= current
            ):
                enrollment.status = ENROLLMENT_EXPIRED
                enrollment.offered_at = None
                enrollment.seat_expires_at = None
                expired_ids.append(enrollment.id)
                changed = True
        # Flush expirations BEFORE recounting seats/promoting, all within
        # the same course-locked transaction.
        db.flush()
        if changed:
            promoted_ids.extend(_promote_waitlist(db, cid, current))
        db.flush()

    return {
        "now": current.isoformat(),
        "expired_enrollment_ids": expired_ids,
        "promoted_enrollment_ids": promoted_ids,
        "courses_scanned": len(course_ids),
    }


def waitlist_position(db: Session, *, course_id: int, learner_id: int) -> int | None:
    """1-based FIFO position of a waitlisted learner, else None."""
    enrollment = db.scalar(
        select(Enrollment).where(
            Enrollment.course_id == course_id,
            Enrollment.learner_id == learner_id,
        )
    )
    if enrollment is None or enrollment.status != ENROLLMENT_WAITLISTED:
        return None
    rank = db.scalar(
        select(func.count())
        .select_from(Enrollment)
        .where(
            Enrollment.course_id == course_id,
            Enrollment.status == ENROLLMENT_WAITLISTED,
            Enrollment.waitlist_position <= enrollment.waitlist_position,
        )
    )
    return rank
