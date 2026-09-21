import uuid

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas import CorrectionCreate, ResultOut, ResultSubmit
from ..security import CurrentUser, get_current_user, require_supervisor
from ..services import progress as svc

router = APIRouter(tags=["progress"])


@router.post("/enrollments/{enrollment_id}/results", response_model=ResultOut, status_code=201)
def submit_result(
    enrollment_id: uuid.UUID,
    body: ResultSubmit,
    response: Response,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    result, created = svc.submit_result(
        db, enrollment_id, user.user_id, body.step_key, body.passed
    )
    if not created:
        response.status_code = 200  # idempotent replay of an existing result
    return result


@router.post("/results/{result_id}/corrections", response_model=ResultOut)
def correct_result(
    result_id: uuid.UUID,
    body: CorrectionCreate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_supervisor),
):
    return svc.correct_result(db, result_id, user.user_id, body.passed, body.reason)
