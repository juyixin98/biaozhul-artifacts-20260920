"""Directory endpoints: units, coordinators, workers with qualifications."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_coordinator
from app.api.serializers import coordinator_out, worker_out
from app.clock import clock
from app.database import get_db
from app.models import CareWorker, Coordinator, Qualification, Unit
from app.schemas import (
    CoordinatorOut,
    QualificationIn,
    UnitOut,
    WorkerIn,
    WorkerOut,
)

router = APIRouter(prefix="/directory", tags=["directory"])


@router.post("/units", response_model=UnitOut, status_code=201)
def create_unit(name: str, db: Session = Depends(get_db)):
    unit = Unit(name=name)
    db.add(unit)
    db.commit()
    db.refresh(unit)
    return unit


@router.get("/units", response_model=list[UnitOut])
def list_units(db: Session = Depends(get_db)):
    return list(db.scalars(select(Unit).order_by(Unit.id)))


@router.post("/coordinators", response_model=CoordinatorOut, status_code=201)
def create_coordinator(
    name: str, unit_ids: str = "", db: Session = Depends(get_db)
):
    """Create a coordinator; ``unit_ids`` is a comma-separated allow-list."""
    ids = {int(x) for x in unit_ids.split(",") if x.strip()}
    units = list(db.scalars(select(Unit).where(Unit.id.in_(ids)))) if ids else []
    coordinator = Coordinator(name=name, units=units, created_at=clock.now())
    db.add(coordinator)
    db.commit()
    db.refresh(coordinator)
    return coordinator_out(coordinator)


@router.get("/coordinators/me", response_model=CoordinatorOut)
def me(coordinator=Depends(require_coordinator)):
    return coordinator_out(coordinator)


@router.post("/workers", response_model=WorkerOut, status_code=201)
def create_worker(payload: WorkerIn, db: Session = Depends(get_db)):
    if db.get(Unit, payload.unit_id) is None:
        raise HTTPException(status_code=404, detail="unit not found")
    worker = CareWorker(
        name=payload.name,
        unit_id=payload.unit_id,
        timezone=payload.timezone,
        active=True,
        created_at=clock.now(),
    )
    db.add(worker)
    db.commit()
    db.refresh(worker)
    return worker_out(worker)


@router.get("/workers", response_model=list[WorkerOut])
def list_workers(unit_id: int | None = None, db: Session = Depends(get_db)):
    stmt = select(CareWorker).order_by(CareWorker.id)
    if unit_id is not None:
        stmt = stmt.where(CareWorker.unit_id == unit_id)
    return [worker_out(w) for w in db.scalars(stmt)]


@router.get("/workers/{worker_id}", response_model=WorkerOut)
def get_worker(worker_id: int, db: Session = Depends(get_db)):
    worker = db.get(CareWorker, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="worker not found")
    return worker_out(worker)


@router.post("/workers/{worker_id}/qualifications", response_model=WorkerOut,
             status_code=201)
def add_qualification(worker_id: int, payload: QualificationIn,
                      db: Session = Depends(get_db)):
    worker = db.get(CareWorker, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="worker not found")
    if payload.valid_until <= payload.valid_from:
        raise HTTPException(status_code=422,
                            detail="valid_until must be after valid_from")
    db.add(
        Qualification(
            worker_id=worker_id,
            code=payload.code,
            valid_from=payload.valid_from,
            valid_until=payload.valid_until,
        )
    )
    db.commit()
    db.refresh(worker)
    return worker_out(worker)
