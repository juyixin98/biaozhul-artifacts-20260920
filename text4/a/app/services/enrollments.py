"""Enrollment, seat holding, waitlist and 48h expiry.

Concurrency model
-----------------
Every operation that moves a seat in a course first takes a row-level lock on
that ``courses`` row (``SELECT ... FOR UPDATE``). Postgres then serializes all
seat changes for the course, which makes the capacity check and the seat
assignment atomic: two learners racing for the last seat are processed one
after the other, and the second one lands on the waitlist.

Two database constraints back this up:

* ``ux_enrollment_learner_version`` makes repeated sign-up idempotent;
* ``ux_enrollment_seat_number`` guarantees a seat slot 1..capacity is held by
  at most one enrollment even if application code has a bug.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..config import get_settings
from ..errors import AppError, conflict, not_found, unprocessable
from ..models import (
    Certificate,
    CertificateStatus,
    Course,
    Enrollment,
    EnrollmentStatus,
    ProgramVersion,
    User,
    VersionStatus,
)
from ..schemas import CourseCreate


# ---------------------------------------------------------------- courses ---


def create_course(db: Session, manager: User, payload: CourseCreate) -> Course:
    version = db.get(ProgramVersion, payload.version_id)
    if version is None:
        raise not_found("version not found")
    if version.status is not VersionStatus.PUBLISHED:
        raise unprocessable("VERSION_NOT_PUBLISHED", "courses can only offer published versions")
    if db.scalar(select(Course.id).where(Course.version_id == version.id)):
        raise conflict("COURSE_EXISTS", "this version already has a course")

    deadline = payload.enrollment_deadline
    if deadline.tzinfo is None:
        from datetime import timezone

        deadline = deadline.replace(tzinfo=timezone.utc)

    course = Course(
        version_id=version.id,
        capacity=payload.capacity,
        enrollment_deadline=deadline,
        created_at=utcnow(db),
    )
    db.add(course)
    db.commit()
    db.refresh(course)
    return course


def get_course(db: Session, course_id: int) -> Course:
    course = db.get(Course, course_id)
    if course is None:
        raise not_found("course not found")
    return course


def _lock_course(db: Session, course_id: int) -> Course:
    course = db.scalar(select(Course).where(Course.id == course_id).with_for_update())
    if course is None:
        raise not_found("course not found")
    return course


def _count_seats_taken(db: Session, course_id: int) -> int:
    from sqlalchemy import func

    return db.scalar(
        select(func.count())
        .select_from(Enrollment)
        .where(
            Enrollment.course_id == course_id,
            Enrollment.status.in_([EnrollmentStatus.ENROLLED, EnrollmentStatus.CONFIRMED]),
        )
    )


def _free_seat_number(db: Session, course: Course) -> int:
    """Lowest slot in 1..capacity not currently held."""
    held = set(
        db.scalars(
            select(Enrollment.seat_number).where(
                Enrollment.course_id == course.id,
                Enrollment.status.in_([EnrollmentStatus.ENROLLED, EnrollmentStatus.CONFIRMED]),
            )
        )
    )
    for number in range(1, course.capacity + 1):
        if number not in held:
            return number
    raise RuntimeError("no free seat while under capacity")


# ------------------------------------------------------------ enrollment ---


def enroll(db: Session, learner: User, course_id: int) -> Enrollment:
    now = utcnow(db)
    course = _lock_course(db, course_id)

    # Free any lapsed holds and promote waitlisters before deciding capacity.
    _expire_and_promote(db, course, now)

    existing = db.scalar(
        select(Enrollment)
        .where(
            Enrollment.learner_id == learner.id,
            Enrollment.version_id == course.version_id,
        )
        .with_for_update()
    )
    if existing is not None and existing.status in (
        EnrollmentStatus.ENROLLED,
        EnrollmentStatus.CONFIRMED,
        EnrollmentStatus.WAITLISTED,
    ):
        # Idempotent: repeat sign-up returns the same enrollment unchanged,
        # even after the deadline has passed.
        db.commit()
        db.refresh(existing)
        return existing

    if now > course.enrollment_deadline:
        raise unprocessable(
            "ENROLLMENT_CLOSED", "the enrollment deadline for this course has passed"
        )

    hold_hours = get_settings().seat_hold_hours
    taken = _count_seats_taken(db, course.id)

    if existing is None:
        enrollment = Enrollment(
            learner_id=learner.id,
            version_id=course.version_id,
            course_id=course.id,
            created_at=now,
            updated_at=now,
        )
        db.add(enrollment)
    else:
        # Re-signing after a previous cancellation/expiry reuses the row.
        enrollment = existing

    if taken < course.capacity:
        enrollment.status = EnrollmentStatus.ENROLLED
        enrollment.seat_number = _free_seat_number(db, course)
        enrollment.waitlist_position = None
        enrollment.seat_expires_at = now + timedelta(hours=hold_hours)
        enrollment.confirmed_at = None
        enrollment.cancelled_at = None
    else:
        enrollment.status = EnrollmentStatus.WAITLISTED
        enrollment.seat_number = None
        enrollment.seat_expires_at = None
        enrollment.waitlist_position = _next_waitlist_position(db, course.id)
        enrollment.confirmed_at = None
        enrollment.cancelled_at = None

    enrollment.updated_at = now
    db.commit()
    db.refresh(enrollment)
    return enrollment


def confirm(db: Session, learner: User, enrollment_id: int) -> Enrollment:
    now = utcnow(db)
    enrollment = _get_owned_enrollment(db, learner, enrollment_id, lock_enrollment=False)
    course = _lock_course(db, enrollment.course_id)
    db.refresh(enrollment, with_for_update=True)

    _expire_and_promote(db, course, now)

    if enrollment.status is EnrollmentStatus.CONFIRMED:
        db.commit()
        return enrollment  # idempotent
    if enrollment.status is not EnrollmentStatus.ENROLLED:
        raise conflict(
            "SEAT_NOT_HELD",
            f"cannot confirm an enrollment in status {enrollment.status.value}",
        )

    enrollment.status = EnrollmentStatus.CONFIRMED
    enrollment.confirmed_at = now
    enrollment.seat_expires_at = None
    enrollment.updated_at = now
    db.commit()
    db.refresh(enrollment)
    return enrollment


def cancel(db: Session, learner: User, enrollment_id: int) -> Enrollment:
    now = utcnow(db)
    enrollment = _get_owned_enrollment(db, learner, enrollment_id, lock_enrollment=False)
    course = _lock_course(db, enrollment.course_id)
    db.refresh(enrollment, with_for_update=True)

    if enrollment.status in (EnrollmentStatus.CANCELLED, EnrollmentStatus.EXPIRED):
        raise conflict(
            "ENROLLMENT_TERMINAL",
            f"enrollment is already {enrollment.status.value}",
        )

    held_seat = enrollment.status in (
        EnrollmentStatus.ENROLLED,
        EnrollmentStatus.CONFIRMED,
    )
    enrollment.status = EnrollmentStatus.CANCELLED
    enrollment.cancelled_at = now
    enrollment.seat_number = None
    enrollment.seat_expires_at = None
    enrollment.waitlist_position = None
    enrollment.updated_at = now
    db.flush()

    # A certificate only exists for a held (confirmed) seat; cancelling it
    # must invalidate that certificate.
    certificate = db.scalar(
        select(Certificate).where(
            Certificate.enrollment_id == enrollment.id,
            Certificate.status == CertificateStatus.VALID,
        )
    )
    if certificate is not None:
        certificate.status = CertificateStatus.REVOKED
        certificate.revoked_at = now
        certificate.revoke_reason = "invalidated automatically: enrollment cancelled"

    if held_seat:
        _promote_waitlisters(db, course, now, seats=1)
    else:
        _compact_waitlist(db, course.id)

    db.commit()
    db.refresh(enrollment)
    return enrollment


# --------------------------------------------------------------- expiry ----


def expire_due_seats(db: Session, course_id: int | None = None) -> int:
    """Release every hold older than 48h and promote eligible waitlisters.

    Returns the number of enrollments that expired.
    """
    now = utcnow(db)
    if course_id is not None:
        courses = [_lock_course(db, course_id)]
    else:
        courses = list(db.scalars(select(Course).order_by(Course.id).with_for_update()))

    total = 0
    for course in courses:
        total += _expire_and_promote(db, course, now)
    db.commit()
    return total


def _expire_and_promote(db: Session, course: Course, now: datetime) -> int:
    """Must be called while holding the course lock. Returns expired count."""
    due = list(
        db.scalars(
            select(Enrollment)
            .where(
                Enrollment.course_id == course.id,
                Enrollment.status == EnrollmentStatus.ENROLLED,
                Enrollment.seat_expires_at <= now,
            )
            .order_by(Enrollment.seat_expires_at)
            .with_for_update()
        )
    )
    for enrollment in due:
        enrollment.status = EnrollmentStatus.EXPIRED
        enrollment.seat_number = None
        enrollment.seat_expires_at = None
        enrollment.waitlist_position = None
        enrollment.updated_at = now

    if due:
        db.flush()
        _promote_waitlisters(db, course, now, seats=len(due))
    return len(due)


def _promote_waitlisters(db: Session, course: Course, now: datetime, seats: int) -> None:
    """Promote up to ``seats`` eligible waitlisters in sign-up order."""
    hold_hours = get_settings().seat_hold_hours
    promoted = 0
    while promoted < seats:
        candidate = db.scalar(
            select(Enrollment)
            .where(
                Enrollment.course_id == course.id,
                Enrollment.status == EnrollmentStatus.WAITLISTED,
            )
            .order_by(Enrollment.waitlist_position)
            .with_for_update()
        )
        if candidate is None or not _eligible(candidate):
            break

        taken = _count_seats_taken(db, course.id)
        if taken >= course.capacity:
            break

        candidate.status = EnrollmentStatus.ENROLLED
        candidate.seat_number = _free_seat_number(db, course)
        candidate.waitlist_position = None
        candidate.seat_expires_at = now + timedelta(hours=hold_hours)
        candidate.updated_at = now
        db.flush()
        promoted += 1

    if promoted:
        _compact_waitlist(db, course.id)


def _eligible(enrollment: Enrollment) -> bool:
    """Eligibility rule for promotion: still queued (defensive check).

    The sign-up deadline only governs joining the queue; waitlisters who
    signed up before it remain eligible for promotion afterwards.
    """
    return enrollment.status is EnrollmentStatus.WAITLISTED


def _next_waitlist_position(db: Session, course_id: int) -> int:
    from sqlalchemy import func

    return (
        db.scalar(
            select(func.coalesce(func.max(Enrollment.waitlist_position), 0)).where(
                Enrollment.course_id == course_id,
                Enrollment.status == EnrollmentStatus.WAITLISTED,
            )
        )
        + 1
    )


def _compact_waitlist(db: Session, course_id: int) -> None:
    rows = list(
        db.scalars(
            select(Enrollment)
            .where(
                Enrollment.course_id == course_id,
                Enrollment.status == EnrollmentStatus.WAITLISTED,
            )
            .order_by(Enrollment.waitlist_position)
            .with_for_update()
        )
    )
    for position, row in enumerate(rows, start=1):
        row.waitlist_position = position
    db.flush()


def _get_owned_enrollment(
    db: Session, learner: User, enrollment_id: int, *, lock_enrollment: bool
) -> Enrollment:
    stmt = select(Enrollment).where(Enrollment.id == enrollment_id)
    if lock_enrollment:
        stmt = stmt.with_for_update()
    enrollment = db.scalar(stmt)
    if enrollment is None:
        raise not_found("enrollment not found")
    if enrollment.learner_id != learner.id:
        raise AppError(403, "NOT_OWN_ENROLLMENT", "learners may only act on their own enrollment")
    return enrollment


def get_owned_enrollment(db: Session, learner: User, enrollment_id: int) -> Enrollment:
    return _get_owned_enrollment(db, learner, enrollment_id, lock_enrollment=False)
