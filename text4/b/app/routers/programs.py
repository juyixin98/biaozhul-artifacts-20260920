"""Supervisor endpoints: programs, versions, steps, publish, rollback."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import current_user, get_db, require_supervisor
from app.models import ProgramVersion, User
from app.schemas.dto import (
    DraftCreate,
    ProgramCreate,
    ProgramResponse,
    ReplaceStepsRequest,
    VersionResponse,
)
from app.serializers import serialize_version
from app.services import versions_service

router = APIRouter(prefix="/api/programs", tags=["programs"])


@router.post("", response_model=ProgramResponse, status_code=201)
def create_program(
    payload: ProgramCreate,
    session: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> ProgramResponse:
    program = versions_service.create_program(
        session,
        supervisor_id=user.id,
        title=payload.title,
        description=payload.description,
        capacity=payload.capacity,
        enrollment_deadline=payload.enrollment_deadline,
    )
    return ProgramResponse.model_validate(program)


@router.get("/{program_id}", response_model=ProgramResponse)
def get_program(
    program_id: int,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ProgramResponse:
    program = versions_service.get_program_or_404(session, program_id)
    return ProgramResponse.model_validate(program)


@router.get("/{program_id}/versions", response_model=list[VersionResponse])
def list_versions(
    program_id: int,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[VersionResponse]:
    program = versions_service.get_program_or_404(session, program_id)
    versions = sorted(program.versions, key=lambda v: v.version_number)
    return [serialize_version(session, v) for v in versions]


@router.get("/versions/{version_id}", response_model=VersionResponse)
def get_version(
    version_id: int,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> VersionResponse:
    version = session.get(ProgramVersion, version_id)
    if version is None:
        from app.errors import NotFoundError

        raise NotFoundError(f"version {version_id} not found")
    return serialize_version(session, version)


@router.post("/{program_id}/drafts", response_model=VersionResponse, status_code=201)
def create_draft(
    program_id: int,
    payload: DraftCreate,
    session: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> VersionResponse:
    draft = versions_service.create_draft(
        session,
        program_id,
        supervisor_id=user.id,
        base_version_id=payload.base_version_id,
    )
    return serialize_version(session, draft)


@router.put("/versions/{version_id}/steps", response_model=VersionResponse)
def replace_steps(
    version_id: int,
    payload: ReplaceStepsRequest,
    session: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> VersionResponse:
    version = versions_service.replace_steps(
        session, version_id, payload.steps, supervisor_id=user.id
    )
    return serialize_version(session, version)


@router.post("/versions/{version_id}/publish", response_model=VersionResponse)
def publish(
    version_id: int,
    session: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> VersionResponse:
    version = versions_service.publish_version(session, version_id, supervisor_id=user.id)
    return serialize_version(session, version)


@router.post("/{program_id}/rollback/{version_id}", response_model=ProgramResponse)
def rollback(
    program_id: int,
    version_id: int,
    session: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
) -> ProgramResponse:
    program = versions_service.rollback(
        session, program_id, version_id, supervisor_id=user.id
    )
    return ProgramResponse.model_validate(program)
