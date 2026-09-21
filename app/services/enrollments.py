"""Enrollment lifecycle: seats, waitlist, confirmation window, expiry.

Concurrency model: every operation that changes seat accounting takes a
``SELECT ... FOR UPDATE`` lock on the course row first, which serializes
enroll / confirm / cancel / expire for that course. ``seats_taken`` counts
pending + confirmed enrollments; waitlisted ones hold no seat. The unique
(course_id, learner_id) constraint plus the course lock makes duplicate
enrollment idempotent and prevents overselling the last seat.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..config import SEAT_CONFIRM_TTL_HOURS
from ..errors import DomainError
from ..models import (
    ENROLLMENT_CANCELLED,
    ENROLLMENT_CONFIRMED,
    ENROLLMENT_EXPIRED,
    ENROLLMENT_PENDING,
    ENROLLMENT_WAITLISTED,
    VERSION_PUBLISHED,
    Course,
    Enrollment,
    ProgramVersion,
)

HOLD_TTL = timedelta(hours=SEAT_CONFIRM_TTL_HOURS)

_ACTIVE = (ENROLLMENT_PENDING, ENROLLMENT_CONFIRMED, ENROLLMENT_WAITLISTED)
_SEAT_HOLDING = (ENROLLMENT_PENDING, ENROLLMENT_CONFIRMED)


def _lock_course(db: Session, course_id: uuid.UUID) -> Course:
    course = db.scalar(
        select(Course).where(Course.id == course_id).with_for_update()
    )
    if course is None:
        raise DomainError(404, "course not found")
    return course


def _lock_enrollment(db: Session, enrollment_id: uuid.UUID) -> Enrollment:
    enrollment = db.scalar(
        select(Enrollment).where(Enrollment.id == enrollment_id).with_for_update()
    )
    if enrollment is None:
        raise DomainError(404, "enrollment not found")
    return enrollment


def _promote_waitlist(db: Session, course: Course) -> None:
    """Fill free seats from the waitlist, oldest enrollment first."""
    now = clock.now()
    while course.seats_taken < course.capacity:
        nxt = db.scalar(
            select(Enrollment)
            .where(
                Enrollment.course_id == course.id,
                Enrollment.status == ENROLLMENT_WAITLISTED,
            )
            .order_by(Enrollment.created_at, Enrollment.id)
            .limit(1)
            .with_for_update()
        )
        if nxt is None:
            break
        nxt.status = ENROLLMENT_PENDING
        nxt.confirm_deadline = now + HOLD_TTL
        course.seats_taken += 1


def _release_seat(db: Session, enrollment: Enrollment, held_seat: bool) -> None:
    course = _lock_course(db, enrollment.course_id)
    if held_seat and course.seats_taken > 0:
        course.seats_taken -= 1
    _promote_waitlist(db, course)


def enroll(db: Session, course_id: uuid.UUID, learner_id: str,
           version_id: uuid.UUID | None = None) -> Enrollment:
    course = _lock_course(db, course_id)
    now = clock.now()

    existing = db.scalar(
        select(Enrollment).where(
            Enrollment.course_id == course.id,
            Enrollment.learner_id == learner_id,
        )
    )
    if existing is not None and existing.status in _ACTIVE:
        return existing  # idempotent duplicate enrollment

    if now > course.enrollment_deadline:
        raise DomainError(409, "enrollment deadline has passed")

    program = course.program
    vid = version_id or program.current_version_id
    version = db.get(ProgramVersion, vid) if vid else None
    if (
        version is None
        or version.program_id != program.id
        or version.status != VERSION_PUBLISHED
    ):
        raise DomainError(400, "no published program version available to enroll in")

    enrollment = existing or Enrollment(course_id=course.id, learner_id=learner_id)
    enrollment.version_id = version.id
    enrollment.created_at = now
    enrollment.confirmed_at = None
    if course.seats_taken < course.capacity:
        enrollment.status = ENROLLMENT_PENDING
        enrollment.confirm_deadline = now + HOLD_TTL
        course.seats_taken += 1
    else:
        enrollment.status = ENROLLMENT_WAITLISTED
        enrollment.confirm_deadline = None
    db.add(enrollment)
    db.commit()
    db.refresh(enrollment)
    return enrollment


def confirm(db: Session, enrollment_id: uuid.UUID, learner_id: str) -> Enrollment:
    enrollment = _lock_enrollment(db, enrollment_id)
    if enrollment.learner_id != learner_id:
        raise DomainError(403, "only the enrolled learner can confirm this seat")
    if enrollment.status == ENROLLMENT_CONFIRMED:
        return enrollment  # idempotent
    if enrollment.status != ENROLLMENT_PENDING:
        raise DomainError(
            409, f"cannot confirm an enrollment in status '{enrollment.status}'"
        )
    if clock.now() > enrollment.confirm_deadline:
        # The 48h window lapsed between listing and confirming: expire now so
        # seat accounting stays consistent, then tell the caller.
        enrollment.status = ENROLLMENT_EXPIRED
        enrollment.confirm_deadline = None
        _release_seat(db, enrollment, held_seat=True)
        db.commit()
        raise DomainError(409, "confirmation window has expired; seat was released")
    enrollment.status = ENROLLMENT_CONFIRMED
    enrollment.confirmed_at = clock.now()
    enrollment.confirm_deadline = None
    db.commit()
    db.refresh(enrollment)
    return enrollment


def cancel(db: Session, enrollment_id: uuid.UUID) -> Enrollment:
    enrollment = _lock_enrollment(db, enrollment_id)
    if enrollment.status in (ENROLLMENT_CANCELLED, ENROLLMENT_EXPIRED):
        return enrollment  # idempotent
    held_seat = enrollment.status in _SEAT_HOLDING
    enrollment.status = ENROLLMENT_CANCELLED
    enrollment.confirm_deadline = None
    if held_seat:
        _release_seat(db, enrollment, held_seat=True)
    db.commit()
    db.refresh(enrollment)
    return enrollment


def expire_overdue(db: Session) -> int:
    """Release seats whose 48h confirmation window has lapsed.

    Returns the number of enrollments expired. Safe to run concurrently:
    each enrollment and course row is locked before mutation.
    """
    now = clock.now()
    ids = db.scalars(
        select(Enrollment.id).where(
            Enrollment.status == ENROLLMENT_PENDING,
            Enrollment.confirm_deadline.isnot(None),
            Enrollment.confirm_deadline < now,
        )
    ).all()
    expired = 0
    for enrollment_id in ids:
        enrollment = _lock_enrollment(db, enrollment_id)
        if (
            enrollment.status != ENROLLMENT_PENDING
            or enrollment.confirm_deadline is None
            or enrollment.confirm_deadline >= now
        ):
            continue  # confirmed/cancelled concurrently
        enrollment.status = ENROLLMENT_EXPIRED
        enrollment.confirm_deadline = None
        _release_seat(db, enrollment, held_seat=True)
        expired += 1
    db.commit()
    return expired
