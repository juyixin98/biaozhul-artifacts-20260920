from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import current_manager
from ..models import Program, User
from ..schemas import ProgramCreate, ProgramOut, VersionCreate, VersionOut
from ..serializers import program_out, version_out
from ..services import programs as service

router = APIRouter(tags=["programs"])


@router.post("/programs", response_model=ProgramOut, status_code=201)
def create_program(
    payload: ProgramCreate,
    db: Session = Depends(get_db),
    manager: User = Depends(current_manager),
) -> ProgramOut:
    return program_out(service.create_program(db, manager, payload))


@router.get("/programs/{program_id}", response_model=ProgramOut)
def get_program(program_id: int, db: Session = Depends(get_db)) -> ProgramOut:
    return program_out(service.get_program(db, program_id))


@router.get("/programs", response_model=list[ProgramOut])
def list_programs(db: Session = Depends(get_db)) -> list[ProgramOut]:
    return [program_out(p) for p in db.scalars(select(Program).order_by(Program.id))]


@router.post("/programs/{program_id}/versions", response_model=VersionOut, status_code=201)
def create_version(
    program_id: int,
    payload: VersionCreate,
    db: Session = Depends(get_db),
    manager: User = Depends(current_manager),
) -> VersionOut:
    return version_out(db, service.create_version(db, manager, program_id, payload))


@router.get("/programs/{program_id}/versions", response_model=list[VersionOut])
def list_versions(program_id: int, db: Session = Depends(get_db)) -> list[VersionOut]:
    return [version_out(db, v) for v in service.list_versions(db, program_id)]


@router.get("/versions/{version_id}", response_model=VersionOut)
def get_version(version_id: int, db: Session = Depends(get_db)) -> VersionOut:
    return version_out(db, service.get_version(db, version_id))


@router.post("/versions/{version_id}/publish", response_model=VersionOut)
def publish_version(
    version_id: int,
    db: Session = Depends(get_db),
    manager: User = Depends(current_manager),
) -> VersionOut:
    return version_out(db, service.publish_version(db, manager, version_id))


class RollbackIn(BaseModel):
    version_id: int


@router.put("/programs/{program_id}/current-version", response_model=ProgramOut)
def rollback_pointer(
    program_id: int,
    payload: RollbackIn,
    db: Session = Depends(get_db),
    manager: User = Depends(current_manager),
) -> ProgramOut:
    return program_out(service.rollback_pointer(db, manager, program_id, payload.version_id))
