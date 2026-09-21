import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas import (
    ProgramCreate,
    ProgramOut,
    RollbackRequest,
    StepsReplace,
    VersionCreate,
    VersionOut,
)
from ..security import CurrentUser, require_supervisor
from ..services import programs as svc

router = APIRouter(tags=["programs"])


@router.post("/programs", response_model=ProgramOut, status_code=201)
def create_program(
    body: ProgramCreate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_supervisor),
):
    return svc.create_program(
        db, user.user_id, body.title, body.description, body.steps, body.change_note
    )


@router.get("/programs/{program_id}", response_model=ProgramOut)
def get_program(program_id: uuid.UUID, db: Session = Depends(get_db)):
    return svc.get_program(db, program_id)


@router.get("/programs/{program_id}/versions", response_model=list[VersionOut])
def list_versions(program_id: uuid.UUID, db: Session = Depends(get_db)):
    return svc.get_program(db, program_id).versions


@router.post("/programs/{program_id}/versions", response_model=VersionOut, status_code=201)
def create_version(
    program_id: uuid.UUID,
    body: VersionCreate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_supervisor),
):
    return svc.create_version(db, program_id, body.steps, body.change_note)


@router.put("/versions/{version_id}/steps", response_model=VersionOut)
def replace_draft_steps(
    version_id: uuid.UUID,
    body: StepsReplace,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_supervisor),
):
    return svc.replace_draft_steps(db, version_id, body.steps)


@router.post("/versions/{version_id}/publish", response_model=VersionOut)
def publish_version(
    version_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_supervisor),
):
    return svc.publish_version(db, version_id)


@router.get("/versions/{version_id}", response_model=VersionOut)
def get_version(version_id: uuid.UUID, db: Session = Depends(get_db)):
    return svc.get_version(db, version_id)


@router.post("/programs/{program_id}/rollback", response_model=ProgramOut)
def rollback_program(
    program_id: uuid.UUID,
    body: RollbackRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_supervisor),
):
    return svc.rollback_program(db, program_id, body.version_number)
