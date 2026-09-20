from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import current_learner, current_manager, current_user
from ..models import User
from ..schemas import (
    CertificateOut,
    CorrectionCreate,
    ProgressOut,
    StepResultOut,
    SubmissionCreate,
)
from ..serializers import certificate_out
from ..services import progress as service

router = APIRouter(tags=["progress"])


@router.post(
    "/enrollments/{enrollment_id}/steps/{step_order}/submissions",
    response_model=StepResultOut,
    status_code=201,
)
def submit(
    enrollment_id: int,
    step_order: int,
    payload: SubmissionCreate,
    db: Session = Depends(get_db),
    learner: User = Depends(current_learner),
) -> StepResultOut:
    result = service.submit_result(db, learner, enrollment_id, step_order, payload)
    return _result_out(result, step_order)


@router.post(
    "/enrollments/{enrollment_id}/steps/{step_order}/corrections",
    response_model=StepResultOut,
)
def correct(
    enrollment_id: int,
    step_order: int,
    payload: CorrectionCreate,
    db: Session = Depends(get_db),
    manager: User = Depends(current_manager),
) -> StepResultOut:
    result = service.correct_result(db, manager, enrollment_id, step_order, payload)
    return _result_out(result, step_order)


@router.get("/enrollments/{enrollment_id}/progress", response_model=ProgressOut)
def progress(
    enrollment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ProgressOut:
    return service.get_progress(db, user, enrollment_id)


@router.get("/enrollments/{enrollment_id}/certificate", response_model=CertificateOut)
def certificate(
    enrollment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> CertificateOut:
    return certificate_out(service.get_certificate(db, user, enrollment_id))


def _result_out(result, step_order: int) -> StepResultOut:
    return StepResultOut(
        step_id=result.step_id,
        step_order=step_order,
        status=result.status,
        attempt_count=result.attempt_count,
        passed_at=result.passed_at,
        correction_reason=result.correction_reason,
        corrected_at=result.corrected_at,
    )
