from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Coordinator, CoordinatorUnit
from app.schemas import CoordinatorCreate, CoordinatorOut

router = APIRouter(prefix="/coordinators", tags=["coordinators"])


@router.post("", response_model=CoordinatorOut, status_code=201)
def create_coordinator(body: CoordinatorCreate, db: Session = Depends(get_db)):
    coordinator = Coordinator(name=body.name)
    db.add(coordinator)
    db.flush()
    for unit_id in body.unit_ids:
        db.add(CoordinatorUnit(coordinator_id=coordinator.id, unit_id=unit_id))
    db.commit()
    db.refresh(coordinator)
    return coordinator


@router.get("", response_model=list[CoordinatorOut])
def list_coordinators(db: Session = Depends(get_db)):
    return db.query(Coordinator).order_by(Coordinator.id).all()
