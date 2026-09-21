"""Program / version lifecycle: drafts, publish validation, rollback.

Published versions are immutable. Any change is a new draft version that
must be published explicitly. Enrollments bind to a concrete version id, so
publishing or rolling back never touches learners already in flight.
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clock
from ..config import MAX_STEPS_PER_VERSION
from ..errors import DomainError
from ..models import (
    VERSION_DRAFT,
    VERSION_PUBLISHED,
    Program,
    ProgramVersion,
    Step,
)
from ..schemas import StepIn


def validate_steps(steps: list[StepIn]) -> None:
    """Validate a step list: size, unique keys, known prerequisites, acyclic."""
    if not 1 <= len(steps) <= MAX_STEPS_PER_VERSION:
        raise DomainError(
            422, f"a version must contain between 1 and {MAX_STEPS_PER_VERSION} steps"
        )
    keys = [s.step_key for s in steps]
    if len(set(keys)) != len(keys):
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        raise DomainError(422, f"duplicate step keys: {dupes}")
    keyset = set(keys)
    for s in steps:
        for pre in s.prerequisites:
            if pre not in keyset:
                raise DomainError(
                    422,
                    f"step '{s.step_key}' depends on unknown step '{pre}'",
                )
    # Kahn's algorithm for cycle detection.
    indegree = {k: 0 for k in keys}
    dependents = {k: [] for k in keys}
    for s in steps:
        for pre in s.prerequisites:
            dependents[pre].append(s.step_key)
            indegree[s.step_key] += 1
    queue = [k for k, d in indegree.items() if d == 0]
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        for nxt in dependents[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if visited != len(keys):
        raise DomainError(422, "step prerequisites contain a cycle")


def _materialize_steps(version: ProgramVersion, steps: list[StepIn]) -> None:
    version.steps = [
        Step(
            step_key=s.step_key,
            order_index=i,
            title=s.title,
            instructions=s.instructions,
            pass_condition=s.pass_condition,
            prerequisites=list(s.prerequisites),
        )
        for i, s in enumerate(steps)
    ]


def create_program(db: Session, created_by: str, title: str, description: str,
                   steps: list[StepIn], change_note: str) -> Program:
    validate_steps(steps)
    program = Program(title=title, description=description, created_by=created_by)
    version = ProgramVersion(
        program=program, version_number=1, status=VERSION_DRAFT, change_note=change_note
    )
    _materialize_steps(version, steps)
    db.add(program)
    db.commit()
    db.refresh(program)
    return program


def get_program(db: Session, program_id: uuid.UUID) -> Program:
    program = db.get(Program, program_id)
    if program is None:
        raise DomainError(404, "program not found")
    return program


def get_version(db: Session, version_id: uuid.UUID) -> ProgramVersion:
    version = db.get(ProgramVersion, version_id)
    if version is None:
        raise DomainError(404, "version not found")
    return version


def create_version(db: Session, program_id: uuid.UUID, steps: list[StepIn],
                   change_note: str) -> ProgramVersion:
    validate_steps(steps)
    program = get_program(db, program_id)
    next_number = (
        db.scalar(
            select(func.coalesce(func.max(ProgramVersion.version_number), 0)).where(
                ProgramVersion.program_id == program.id
            )
        )
        + 1
    )
    version = ProgramVersion(
        program_id=program.id,
        version_number=next_number,
        status=VERSION_DRAFT,
        change_note=change_note,
    )
    _materialize_steps(version, steps)
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def replace_draft_steps(db: Session, version_id: uuid.UUID,
                        steps: list[StepIn]) -> ProgramVersion:
    version = get_version(db, version_id)
    if version.status != VERSION_DRAFT:
        raise DomainError(409, "published versions are immutable; create a new version")
    validate_steps(steps)
    version.steps.clear()
    db.flush()
    _materialize_steps(version, steps)
    db.commit()
    db.refresh(version)
    return version


def publish_version(db: Session, version_id: uuid.UUID) -> ProgramVersion:
    version = get_version(db, version_id)
    if version.status == VERSION_PUBLISHED:
        return version  # idempotent
    if version.status != VERSION_DRAFT:
        raise DomainError(409, f"cannot publish a version in status '{version.status}'")
    if not version.steps:
        raise DomainError(422, "cannot publish a version without steps")
    version.status = VERSION_PUBLISHED
    version.published_at = clock.now()
    version.program.current_version_id = version.id
    db.commit()
    db.refresh(version)
    return version


def rollback_program(db: Session, program_id: uuid.UUID,
                     version_number: int) -> Program:
    """Point the program's current version at an older published version.

    Only affects future enrollments; existing enrollments keep the version
    they were bound to.
    """
    program = get_program(db, program_id)
    target = db.scalar(
        select(ProgramVersion).where(
            ProgramVersion.program_id == program.id,
            ProgramVersion.version_number == version_number,
        )
    )
    if target is None:
        raise DomainError(404, f"version {version_number} not found for this program")
    if target.status != VERSION_PUBLISHED:
        raise DomainError(409, "can only roll back to a published version")
    program.current_version_id = target.id
    db.commit()
    db.refresh(program)
    return program
