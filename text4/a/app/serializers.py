"""ORM-to-Schema assembly (steps carry prerequisite *orders*, not row ids)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Certificate,
    Course,
    Enrollment,
    EnrollmentStatus,
    ProgramVersion,
    Step,
    StepDependency,
)
from .schemas import (
    CertificateOut,
    CourseOut,
    EnrollmentOut,
    ProgramOut,
    StepOut,
    UserOut,
    VersionOut,
)


def user_out(user) -> UserOut:
    return UserOut.model_validate(user)


def program_out(program) -> ProgramOut:
    return ProgramOut.model_validate(program)


def _prereq_orders(db: Session, version_id: int) -> dict[int, list[int]]:
    steps = {
        s.id: s.order_index
        for s in db.scalars(select(Step).where(Step.version_id == version_id))
    }
    result: dict[int, list[int]] = {order: [] for order in steps.values()}
    for dep in db.scalars(
        select(StepDependency).where(StepDependency.version_id == version_id)
    ):
        result.setdefault(steps[dep.step_id], []).append(steps[dep.prerequisite_step_id])
    for orders in result.values():
        orders.sort()
    return result


def version_out(db: Session, version: ProgramVersion) -> VersionOut:
    prereqs = _prereq_orders(db, version.id)
    steps = [
        StepOut(
            order_index=s.order_index,
            title=s.title,
            description=s.description,
            pass_criteria=s.pass_criteria,
            prerequisite_orders=prereqs.get(s.order_index, []),
        )
        for s in sorted(version.steps, key=lambda s: s.order_index)
    ]
    return VersionOut(
        id=version.id,
        program_id=version.program_id,
        version=version.version,
        status=version.status,
        content_hash=version.content_hash,
        created_at=version.created_at,
        published_at=version.published_at,
        steps=steps,
    )


def course_out(db: Session, course: Course) -> CourseOut:
    from sqlalchemy import func

    seats = db.scalar(
        select(func.count())
        .select_from(Enrollment)
        .where(
            Enrollment.course_id == course.id,
            Enrollment.status.in_([EnrollmentStatus.ENROLLED, EnrollmentStatus.CONFIRMED]),
        )
    )
    waiting = db.scalar(
        select(func.count())
        .select_from(Enrollment)
        .where(
            Enrollment.course_id == course.id,
            Enrollment.status == EnrollmentStatus.WAITLISTED,
        )
    )
    return CourseOut(
        id=course.id,
        version_id=course.version_id,
        capacity=course.capacity,
        enrollment_deadline=course.enrollment_deadline,
        seats_taken=seats,
        waiting_count=waiting,
    )


def enrollment_out(enrollment: Enrollment) -> EnrollmentOut:
    return EnrollmentOut.model_validate(enrollment)


def certificate_out(certificate: Certificate) -> CertificateOut:
    out = CertificateOut.model_validate(certificate)
    out.status = certificate.status.value
    return out
