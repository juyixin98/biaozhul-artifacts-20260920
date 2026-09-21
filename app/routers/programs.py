from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import CurrentUser, require_supervisor
from app.models import Program
from app.schemas import ProgramCreate, RollbackIn, VersionCreate
from app.services import programs as svc

router = APIRouter(prefix="/programs", tags=["programs"])


@router.post("", status_code=201)
def create_program(
    payload: ProgramCreate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_supervisor),
):
    program = svc.create_program(
        db,
        supervisor_id=user.id,
        title=payload.title,
        description=payload.description,
    )
    db.commit()
    db.refresh(program)
    return program.to_dict()


@router.get("")
def list_programs(db: Session = Depends(get_db)):
    programs = db.scalars(select(Program).order_by(Program.id)).all()
    return [p.to_dict() for p in programs]


@router.get("/{program_id}")
def get_program(program_id: int, db: Session = Depends(get_db)):
    program = svc._get_program(db, program_id)
    data = program.to_dict()
    data["versions"] = [v.to_dict() for v in program.versions]
    return data


@router.post("/{program_id}/versions", status_code=201)
def create_version(
    program_id: int,
    payload: VersionCreate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_supervisor),
):
    version = svc.create_version(
        db,
        supervisor_id=user.id,
        program_id=program_id,
        steps=[s.model_dump() for s in payload.steps],
        publish=payload.publish,
    )
    db.commit()
    version = svc.get_version(db, version.id)
    return version.to_dict(include_steps=True)


@router.post("/versions/{version_id}/publish")
def publish_version(
    version_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_supervisor),
):
    version = svc.publish_version(db, supervisor_id=user.id, version_id=version_id)
    db.commit()
    version = svc.get_version(db, version.id)
    return version.to_dict(include_steps=True)


@router.get("/versions/{version_id}")
def get_version(version_id: int, db: Session = Depends(get_db)):
    version = svc.get_version(db, version_id)
    return version.to_dict(include_steps=True)


@router.post("/{program_id}/rollback")
def rollback(
    program_id: int,
    payload: RollbackIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_supervisor),
):
    program = svc.rollback(
        db,
        supervisor_id=user.id,
        program_id=program_id,
        version_id=payload.version_id,
    )
    db.commit()
    db.refresh(program)
    return program.to_dict()
