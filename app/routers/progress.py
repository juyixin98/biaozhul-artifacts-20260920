from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import CurrentUser, get_current_user, require_learner, require_supervisor
from app.models import Enrollment, StepResult
from app.schemas import CorrectionIn, ReviewIn, SubmissionIn
from app.services import certificates as cert_svc
from app.services import progress as svc

router = APIRouter(prefix="/enrollments", tags=["progress"])


def _owned_enrollment_or_404(db: Session, enrollment_id: int) -> Enrollment:
    enrollment = db.get(Enrollment, enrollment_id)
    if enrollment is None:
        from app.errors import NotFoundError

        raise NotFoundError(f"Enrollment {enrollment_id} not found.")
    return enrollment


@router.post("/{enrollment_id}/steps/{step_key}/submit")
def submit(
    enrollment_id: int,
    step_key: str,
    payload: SubmissionIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_learner),
):
    result = svc.submit_result(
        db,
        enrollment_id=enrollment_id,
        learner_id=user.id,
        step_key=step_key,
        content=payload.content,
    )
    db.commit()
    db.refresh(result)
    return result.to_dict()


@router.post("/{enrollment_id}/steps/{step_key}/review")
def review(
    enrollment_id: int,
    step_key: str,
    payload: ReviewIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_supervisor),
):
    result = svc.review_result(
        db,
        enrollment_id=enrollment_id,
        step_key=step_key,
        passed=payload.passed,
    )
    result.reviewed_by = user.id
    db.commit()
    db.refresh(result)
    return result.to_dict()


@router.post("/{enrollment_id}/steps/{step_key}/correct")
def correct(
    enrollment_id: int,
    step_key: str,
    payload: CorrectionIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_supervisor),
):
    result = svc.correct_result(
        db,
        supervisor_id=user.id,
        enrollment_id=enrollment_id,
        step_key=step_key,
        new_status=payload.new_status,
        reason=payload.reason,
    )
    db.commit()
    db.refresh(result)
    return result.to_dict()


@router.get("/{enrollment_id}/results")
def list_results(
    enrollment_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    _owned_enrollment_or_404(db, enrollment_id)
    results = db.scalars(
        select(StepResult)
        .where(StepResult.enrollment_id == enrollment_id)
        .order_by(StepResult.id)
    ).all()
    return [r.to_dict() for r in results]


@router.get("/{enrollment_id}/certificate")
def get_certificate(
    enrollment_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    enrollment = _owned_enrollment_or_404(db, enrollment_id)
    if user.role == "learner" and enrollment.learner_id != user.id:
        from app.errors import ForbiddenError

        raise ForbiddenError("You can only view your own certificate.")
    cert = cert_svc.certificate_for_enrollment(db, enrollment_id)
    if cert is None:
        from app.errors import NotFoundError

        raise NotFoundError("No certificate has been issued for this enrolment.")
    return cert.to_dict()
