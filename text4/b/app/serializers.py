"""Serialization helpers that turn ORM graphs into response models."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ProgramVersion, Step, StepPrerequisite
from app.schemas.dto import StepResponse, VersionResponse


def _prerequisite_positions(session: Session, steps: list[Step]) -> dict[int, list[int]]:
    if not steps:
        return {}
    ids = [s.id for s in steps]
    rows = session.execute(
        select(StepPrerequisite.step_id, StepPrerequisite.prerequisite_id)
        .where(StepPrerequisite.step_id.in_(ids))
    ).all()
    id_to_position = {s.id: s.position for s in steps}
    mapping: dict[int, list[int]] = {s.id: [] for s in steps}
    for step_id, prereq_id in rows:
        mapping[step_id].append(id_to_position[prereq_id])
    for positions in mapping.values():
        positions.sort()
    return mapping


def serialize_version(session: Session, version: ProgramVersion) -> VersionResponse:
    steps = sorted(version.steps, key=lambda s: s.position)
    prereq_map = _prerequisite_positions(session, steps)
    return VersionResponse(
        id=version.id,
        program_id=version.program_id,
        version_number=version.version_number,
        status=version.status,
        created_at=version.created_at,
        published_at=version.published_at,
        steps=[
            StepResponse(
                id=step.id,
                position=step.position,
                title=step.title,
                instruction=step.instruction,
                pass_condition=step.pass_condition,
                prerequisite_positions=prereq_map[step.id],
            )
            for step in steps
        ],
    )
