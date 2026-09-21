import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import DomainError
from ..models import Enrollment, StepResult
from ..schemas import EnrollmentOut
from ..security import CurrentUser, get_current_user
from ..services import enrollments as svc

router = APIRouter(tags=["enrollments"])


def _load(db: Session, enrollment_id: uuid.UUID) -> Enrollment:
    enrollment = db.get(Enrollment, enrollment_id)
    if enrollment is None:
        raise DomainError(404, "enrollment not found")
    return enrollment


def _check_access(enrollment: Enrollment, user: CurrentUser) -> None:
    if enrollment.learner_id != user.user_id and not user.is_supervisor:
        raise DomainError(403, "not allowed to access this enrollment")


@router.get("/enrollments/{enrollment_id}", response_model=EnrollmentOut)
def get_enrollment(
    enrollment_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    enrollment = _load(db, enrollment_id)
    _check_access(enrollment, user)
    return enrollment


@router.post("/enrollments/{enrollment_id}/confirm", response_model=EnrollmentOut)
def confirm_enrollment(
    enrollment_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    return svc.confirm(db, enrollment_id, user.user_id)


@router.post("/enrollments/{enrollment_id}/cancel", response_model=EnrollmentOut)
def cancel_enrollment(
    enrollment_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    enrollment = _load(db, enrollment_id)
    _check_access(enrollment, user)
    return svc.cancel(db, enrollment_id)


@router.get("/enrollments/{enrollment_id}/progress")
def get_progress(
    enrollment_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    enrollment = _load(db, enrollment_id)
    _check_access(enrollment, user)
    results = {
        r.step_key: r
        for r in db.scalars(
            select(StepResult).where(StepResult.enrollment_id == enrollment.id)
        )
    }
    steps = sorted(enrollment.version.steps, key=lambda s: s.order_index)
    return {
        "enrollment_id": str(enrollment.id),
        "version_id": str(enrollment.version_id),
        "status": enrollment.status,
        "steps": [
            {
                "step_key": s.step_key,
                "order_index": s.order_index,
                "title": s.title,
                "prerequisites": s.prerequisites,
                "passed": results[s.step_key].passed if s.step_key in results else None,
            }
            for s in steps
        ],
    }
