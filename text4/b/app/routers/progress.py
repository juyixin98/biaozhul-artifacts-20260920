"""Step submission, supervisor evaluation/correction and certificate lookup."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import current_user, get_db, require_supervisor
from app.errors import ForbiddenError, NotFoundError
from app.models import Enrollment, User
from app.schemas.dto import (
    CertificateResponse,
    CorrectionRequest,
    StepResultResponse,
    SubmissionCreate,
)
from app.services import progress_service
from app.services.versions_service import get_program_or_404

router = APIRouter(prefix="/api", tags=["progress"])


def _can_view_enrollment(session: Session, enrollment: Enrollment, user: User) -> None:
    if enrollment.learner_id == user.id:
        return
    program = get_program_or_404(session, enrollment.program_id)
    if program.supervisor_id == user.id:
        return
    raise ForbiddenError("not your enrollment")


@router.post(
    "/enrollments/{enrollment_id}/steps/{step_id}/submit",
    response_model=StepResultResponse,
    status_code=201,
)
def submit(
    enrollment_id: int,
    step_id: int,
    payload: SubmissionCreate,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> StepResultResponse:
    if user.role != "learner":
        raise ForbiddenError("only learners submit step results")
    result = progress_service.submit(
        session,
        enrollment_id=enrollment_id,
        step_id=step_id,
        learner_id=user.id,
        content=payload.content,
    )
    return StepResultResponse.model_validate(result)


@router.get("/enrollments/{enrollment_id}/results", response_model=list[StepResultResponse])
def list_results(
    enrollment_id: int,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[StepResultResponse]:
    enrollment = session.get(Enrollment, enrollment_id)
    if enrollment is None:
        raise NotFoundError(f"enrollment {enrollment_id} not found")
    _can_view_enrollment(session, enrollment, user)
    rows = progress_service.list_results(session, enrollment_id)
    return [StepResultResponse.model_validate(r) for r in rows]


@router.put(
    "/enrollments/{enrollment_id}/steps/{step_id}/evaluation",
    response_model=StepResultResponse,
)
def evaluate(
    enrollment_id: int,
    step_id: int,
    payload: CorrectionRequest,
    session: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> StepResultResponse:
    """First evaluation or later correction.

    A ``reason`` is required whenever the result was already evaluated (i.e.
    a correction); the schema always accepts one.
    """
    result = progress_service.evaluate_submission(
        session,
        enrollment_id=enrollment_id,
        step_id=step_id,
        supervisor_id=user.id,
        new_status=payload.status,
        reason=payload.reason,
    )
    return StepResultResponse.model_validate(result)


@router.patch("/enrollments/{enrollment_id}/steps/{step_id}/correct", response_model=StepResultResponse)
def correct(
    enrollment_id: int,
    step_id: int,
    payload: CorrectionRequest,
    session: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> StepResultResponse:
    result = progress_service.correct_result(
        session,
        enrollment_id=enrollment_id,
        step_id=step_id,
        supervisor_id=user.id,
        new_status=payload.status,
        reason=payload.reason,
    )
    return StepResultResponse.model_validate(result)


@router.get("/enrollments/{enrollment_id}/certificate", response_model=CertificateResponse)
def certificate(
    enrollment_id: int,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> CertificateResponse:
    enrollment = session.get(Enrollment, enrollment_id)
    if enrollment is None:
        raise NotFoundError(f"enrollment {enrollment_id} not found")
    _can_view_enrollment(session, enrollment, user)
    cert = progress_service.get_certificate(session, enrollment_id)
    if cert is None:
        raise NotFoundError("no certificate for this enrollment yet")
    return CertificateResponse.model_validate(cert)
