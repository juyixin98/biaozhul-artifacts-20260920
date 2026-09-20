from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import NotFoundError
from app.models import Caregiver, CaregiverQualification
from app.schemas import (CaregiverCreate, CaregiverOut, QualificationCreate,
                         QualificationOut)

router = APIRouter(prefix="/caregivers", tags=["caregivers"])


@router.post("", response_model=CaregiverOut, status_code=201)
def create_caregiver(body: CaregiverCreate, db: Session = Depends(get_db)):
    caregiver = Caregiver(**body.model_dump())
    db.add(caregiver)
    db.commit()
    db.refresh(caregiver)
    return caregiver


@router.get("", response_model=list[CaregiverOut])
def list_caregivers(unit_id: str | None = None, db: Session = Depends(get_db)):
    q = db.query(Caregiver)
    if unit_id:
        q = q.filter(Caregiver.unit_id == unit_id)
    return q.order_by(Caregiver.id).all()


@router.post("/{caregiver_id}/qualifications", response_model=QualificationOut,
             status_code=201)
def add_qualification(caregiver_id: int, body: QualificationCreate,
                      db: Session = Depends(get_db)):
    if db.get(Caregiver, caregiver_id) is None:
        raise NotFoundError(f"caregiver {caregiver_id} not found")
    qual = CaregiverQualification(caregiver_id=caregiver_id, **body.model_dump())
    db.add(qual)
    db.commit()
    db.refresh(qual)
    return qual
