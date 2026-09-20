"""Program and immutable version lifecycle."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..errors import conflict, not_found, unprocessable
from ..models import (
    Program,
    ProgramVersion,
    Step,
    StepDependency,
    User,
    VersionStatus,
)
from ..schemas import ProgramCreate, VersionCreate
from .graph import GraphError, content_hash_for, validate_steps


def create_program(db: Session, manager: User, payload: ProgramCreate) -> Program:
    program = Program(
        title=payload.title,
        description=payload.description,
        created_by=manager.id,
        current_version_id=None,
        created_at=utcnow(db),
    )
    db.add(program)
    db.commit()
    db.refresh(program)
    return program


def get_program(db: Session, program_id: int) -> Program:
    program = db.get(Program, program_id)
    if program is None:
        raise not_found("program not found")
    return program


def get_version(db: Session, version_id: int) -> ProgramVersion:
    version = db.get(ProgramVersion, version_id)
    if version is None:
        raise not_found("version not found")
    return version


def list_versions(db: Session, program_id: int) -> list[ProgramVersion]:
    get_program(db, program_id)
    return list(
        db.scalars(
            select(ProgramVersion)
            .where(ProgramVersion.program_id == program_id)
            .order_by(ProgramVersion.version)
        )
    )


def create_version(
    db: Session, manager: User, program_id: int, payload: VersionCreate
) -> ProgramVersion:
    """Create a new *draft* version.

    Graph legality is enforced at creation as well as at publication, so a
    broken draft cannot be built by accident.
    """
    program = get_program(db, program_id)
    try:
        validate_steps(payload.steps)
    except GraphError as exc:
        raise unprocessable(exc.code, str(exc)) from exc

    now = utcnow(db)
    next_number = (
        db.scalar(
            select(ProgramVersion.version)
            .where(ProgramVersion.program_id == program_id)
            .order_by(ProgramVersion.version.desc())
            .limit(1)
        )
        or 0
    ) + 1

    version = ProgramVersion(
        program_id=program.id,
        version=next_number,
        status=VersionStatus.DRAFT,
        content_hash=content_hash_for(payload.steps),
        created_at=now,
    )
    db.add(version)
    db.flush()

    steps: list[Step] = []
    for i, step_in in enumerate(payload.steps, start=1):
        step = Step(
            version_id=version.id,
            order_index=i,
            title=step_in.title,
            description=step_in.description,
            pass_criteria=step_in.pass_criteria,
        )
        db.add(step)
        steps.append(step)
    db.flush()

    ids_by_order = {step.order_index: step.id for step in steps}
    for i, step_in in enumerate(payload.steps, start=1):
        for pre_order in set(step_in.prerequisite_orders):
            db.add(
                StepDependency(
                    version_id=version.id,
                    step_id=ids_by_order[i],
                    prerequisite_step_id=ids_by_order[pre_order],
                )
            )

    db.commit()
    db.refresh(version)
    return version


def publish_version(db: Session, manager: User, version_id: int) -> ProgramVersion:
    """Validate, freeze and activate a draft version.

    A published version is immutable: steps/dependencies cannot be edited and
    the row never moves back to DRAFT. Any change requires a brand-new version.
    """
    version = get_version(db, version_id)
    if version.status is VersionStatus.PUBLISHED:
        raise conflict("VERSION_ALREADY_PUBLISHED", "published versions are immutable")

    # Re-run the graph check against the persisted rows as the final gate.
    steps = list(
        db.scalars(select(Step).where(Step.version_id == version_id).order_by(Step.order_index))
    )
    deps = db.execute(
        select(StepDependency.step_id, StepDependency.prerequisite_step_id).where(
            StepDependency.version_id == version_id
        )
    ).all()
    step_ids = {s.id: s.order_index for s in steps}
    edges: dict[int, set[int]] = {s.order_index: set() for s in steps}
    for step_id, pre_id in deps:
        if step_id not in step_ids or pre_id not in step_ids:
            raise unprocessable("PREREQUISITE_UNKNOWN", "dangling dependency in stored version")
        edges[step_ids[step_id]].add(step_ids[pre_id])
    _ensure_acyclic(edges, len(steps))

    now = utcnow(db)
    version.status = VersionStatus.PUBLISHED
    version.published_at = now

    program = db.get(Program, version.program_id)
    # Publishing activates the version for *future* enrollments; enrollments
    # already pinned to another version are untouched.
    program.current_version_id = version.id

    db.commit()
    db.refresh(version)
    return version


def rollback_pointer(db: Session, manager: User, program_id: int, version_id: int) -> Program:
    """Point new enrollments at an older published version.

    This is a pointer change only. It never mutates versions or moves
    in-progress learners.
    """
    program = get_program(db, program_id)
    target = get_version(db, version_id)
    if target.program_id != program.id:
        raise unprocessable("VERSION_PROGRAM_MISMATCH", "version does not belong to program")
    if target.status is not VersionStatus.PUBLISHED:
        raise unprocessable("VERSION_NOT_PUBLISHED", "only published versions can be activated")

    program.current_version_id = target.id
    db.commit()
    db.refresh(program)
    return program


def _ensure_acyclic(edges: dict[int, set[int]], n: int) -> None:
    from collections import deque

    indegree = {k: len(v) for k, v in edges.items()}
    dependents: dict[int, list[int]] = {k: [] for k in edges}
    for node, pres in edges.items():
        for pre in pres:
            dependents[pre].append(node)
    queue = deque([i for i, d in indegree.items() if d == 0])
    seen = 0
    while queue:
        node = queue.popleft()
        seen += 1
        for dep in dependents[node]:
            indegree[dep] -= 1
            if indegree[dep] == 0:
                queue.append(dep)
    if seen != n:
        cyclic = sorted(i for i, d in indegree.items() if d > 0)
        raise unprocessable(
            "STEPS_CYCLIC", f"dependency cycle detected involving steps {cyclic}"
        )
