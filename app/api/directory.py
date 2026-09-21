"""Directory endpoints: units, coordinators, workers, qualifications."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import (
    Coordinator,
    CoordinatorUnitGrant,
    Qualification,
    Unit,
    Worker,
    WorkerQualification,
)
from app.schemas import (
    CoordinatorIn,
    CoordinatorOut,
    GrantIn,
    QualificationIn,
    UnitIn,
    UnitOut,
    WorkerIn,
    WorkerOut,
    WorkerQualificationIn,
)
from app.services.timeutils import as_utc

router = APIRouter(prefix="/directory", tags=["directory"])


@router.post("/units", response_model=UnitOut, status_code=201)
def create_unit(payload: UnitIn, db: Session = Depends(get_session)) -> Unit:
    unit = Unit(name=payload.name)
    db.add(unit)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"unit {payload.name!r} already exists")
    return unit


@router.get("/units", response_model=list[UnitOut])
def list_units(db: Session = Depends(get_session)) -> list[Unit]:
    return list(db.scalars(select(Unit).order_by(Unit.id)))


def _to_coordinator_out(coordinator: Coordinator) -> CoordinatorOut:
    return CoordinatorOut(
        id=coordinator.id,
        name=coordinator.name,
        is_admin=coordinator.is_admin,
        granted_unit_ids=[g.unit_id for g in coordinator.unit_grants],
    )


@router.post("/coordinators", response_model=CoordinatorOut, status_code=201)
def create_coordinator(payload: CoordinatorIn, db: Session = Depends(get_session)) -> CoordinatorOut:
    coordinator = Coordinator(name=payload.name, is_admin=payload.is_admin)
    db.add(coordinator)
    db.flush()
    return _to_coordinator_out(coordinator)


@router.get("/coordinators", response_model=list[CoordinatorOut])
def list_coordinators(db: Session = Depends(get_session)) -> list[CoordinatorOut]:
    return [
        _to_coordinator_out(c)
        for c in db.scalars(select(Coordinator).order_by(Coordinator.id))
    ]


@router.post("/coordinators/{coordinator_id}/grants", response_model=CoordinatorOut)
def grant_unit(
    coordinator_id: int,
    payload: GrantIn,
    db: Session = Depends(get_session),
) -> CoordinatorOut:
    coordinator = db.get(Coordinator, coordinator_id)
    if coordinator is None:
        raise HTTPException(404, "coordinator not found")
    if db.get(Unit, payload.unit_id) is None:
        raise HTTPException(404, "unit not found")
    exists = db.scalar(
        select(CoordinatorUnitGrant).where(
            CoordinatorUnitGrant.coordinator_id == coordinator_id,
            CoordinatorUnitGrant.unit_id == payload.unit_id,
        )
    )
    if exists is None:
        db.add(CoordinatorUnitGrant(coordinator_id=coordinator_id, unit_id=payload.unit_id))
        db.flush()
    return _to_coordinator_out(coordinator)


@router.post("/workers", response_model=WorkerOut, status_code=201)
def create_worker(payload: WorkerIn, db: Session = Depends(get_session)) -> Worker:
    if db.get(Unit, payload.unit_id) is None:
        raise HTTPException(404, f"unit {payload.unit_id} not found")
    worker = Worker(name=payload.name, unit_id=payload.unit_id, active=payload.active)
    db.add(worker)
    db.flush()
    return worker


@router.get("/workers", response_model=list[WorkerOut])
def list_workers(unit_id: int | None = None, db: Session = Depends(get_session)) -> list[Worker]:
    stmt = select(Worker).order_by(Worker.id)
    if unit_id is not None:
        stmt = stmt.where(Worker.unit_id == unit_id)
    return list(db.scalars(stmt))


@router.post("/qualifications", status_code=201)
def create_qualification(payload: QualificationIn, db: Session = Depends(get_session)) -> dict:
    qual = Qualification(code=payload.code, name=payload.name or payload.code)
    db.add(qual)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"qualification {payload.code!r} already exists")
    return {"id": qual.id, "code": qual.code, "name": qual.name}


@router.post("/workers/{worker_id}/qualifications", status_code=201)
def add_worker_qualification(
    worker_id: int,
    payload: WorkerQualificationIn,
    db: Session = Depends(get_session),
) -> dict:
    worker = db.get(Worker, worker_id)
    if worker is None:
        raise HTTPException(404, "worker not found")
    qual = db.scalar(select(Qualification).where(Qualification.code == payload.code))
    if qual is None:
        raise HTTPException(404, f"qualification {payload.code!r} does not exist; create it first")
    if payload.valid_until is not None and as_utc(payload.valid_until) <= as_utc(payload.valid_from):
        raise HTTPException(422, "valid_until must be after valid_from")
    grant = WorkerQualification(
        worker_id=worker_id,
        qualification_id=qual.id,
        valid_from=as_utc(payload.valid_from),
        valid_until=as_utc(payload.valid_until) if payload.valid_until else None,
    )
    db.add(grant)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "worker already holds this qualification")
    return {
        "id": grant.id,
        "worker_id": worker_id,
        "qualification": payload.code,
        "valid_from": grant.valid_from.isoformat(),
        "valid_until": grant.valid_until.isoformat() if grant.valid_until else None,
    }
