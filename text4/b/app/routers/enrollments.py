"""Learner endpoints: enrol, waitlist, confirm, cancel."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import current_user, get_db, require_learner
from app.models import Enrollment, User
from app.schemas.dto import EnrollmentResponse
from app.services import enrollment_service

router = APIRouter(prefix="/api", tags=["enrollments"])


@router.post("/programs/{program_id}/enroll", response_model=EnrollmentResponse, status_code=201)
def enroll(
    program_id: int,
    session: Session = Depends(get_db),
    user: User = Depends(require_learner),
) -> EnrollmentResponse:
    enrollment = enrollment_service.enroll(session, program_id, user.id)
    return EnrollmentResponse.model_validate(enrollment)


@router.get("/enrollments/{enrollment_id}", response_model=EnrollmentResponse)
def get_enrollment(
    enrollment_id: int,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> EnrollmentResponse:
    enrollment = session.get(Enrollment, enrollment_id)
    if enrollment is None:
        from app.errors import NotFoundError

        raise NotFoundError(f"enrollment {enrollment_id} not found")
    if enrollment.learner_id != user.id:
        # Learners see only their own enrolment; supervisors may inspect.
        from app.services.versions_service import get_program_or_404

        program = get_program_or_404(session, enrollment.program_id)
        if program.supervisor_id != user.id:
            from app.errors import ForbiddenError

            raise ForbiddenError("not your enrollment")
    return EnrollmentResponse.model_validate(enrollment)


@router.get("/my/enrollments", response_model=list[EnrollmentResponse])
def my_enrollments(
    session: Session = Depends(get_db),
    user: User = Depends(require_learner),
) -> list[EnrollmentResponse]:
    rows = session.scalars(
        select(Enrollment)
        .where(Enrollment.learner_id == user.id)
        .order_by(Enrollment.id)
    ).all()
    return [EnrollmentResponse.model_validate(r) for r in rows]


@router.post("/enrollments/{enrollment_id}/confirm", response_model=EnrollmentResponse)
def confirm(
    enrollment_id: int,
    session: Session = Depends(get_db),
    user: User = Depends(require_learner),
) -> EnrollmentResponse:
    enrollment = enrollment_service.confirm(session, enrollment_id, user.id)
    return EnrollmentResponse.model_validate(enrollment)


@router.post("/enrollments/{enrollment_id}/cancel", response_model=EnrollmentResponse)
def cancel(
    enrollment_id: int,
    session: Session = Depends(get_db),
    user: User = Depends(require_learner),
) -> EnrollmentResponse:
    enrollment = enrollment_service.cancel(session, enrollment_id, user.id)
    return EnrollmentResponse.model_validate(enrollment)
