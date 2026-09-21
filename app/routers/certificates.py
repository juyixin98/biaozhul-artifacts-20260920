import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import DomainError
from ..models import Certificate, Enrollment
from ..schemas import CertificateOut
from ..security import CurrentUser, get_current_user

router = APIRouter(tags=["certificates"])


@router.get("/enrollments/{enrollment_id}/certificate", response_model=CertificateOut)
def get_certificate(
    enrollment_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    enrollment = db.get(Enrollment, enrollment_id)
    if enrollment is None:
        raise DomainError(404, "enrollment not found")
    if enrollment.learner_id != user.user_id and not user.is_supervisor:
        raise DomainError(403, "not allowed to access this enrollment")
    cert = db.scalar(
        select(Certificate).where(Certificate.enrollment_id == enrollment_id)
    )
    if cert is None:
        raise DomainError(404, "no certificate issued for this enrollment")
    return cert


@router.get("/certificates/{serial_number}", response_model=CertificateOut)
def verify_certificate(serial_number: str, db: Session = Depends(get_db)):
    """Public verification endpoint: anyone can check validity + digest."""
    cert = db.scalar(
        select(Certificate).where(Certificate.serial_number == serial_number)
    )
    if cert is None:
        raise DomainError(404, "certificate not found")
    return cert
