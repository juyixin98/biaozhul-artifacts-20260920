"""Program & version lifecycle, including step dependency validation.

Dependency validation rules
---------------------------
* Between 1 and 50 ordered steps per version.
* Every prerequisite must reference an existing step and cannot be the step
  itself.
* The prerequisite graph must be a DAG (acyclic); a topological cycle of any
  length is rejected.
* Rules are enforced both when steps are replaced on a draft and again at
  publish time, because a draft is mutable between the two operations.

Immutability
------------
Published versions are frozen: steps cannot be replaced and the version cannot
be edited.  Any change requires a new draft -> new published version.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import now_utc
from app.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.models import Program, ProgramVersion, Step, StepPrerequisite
from app.schemas.dto import StepIn
from app.statuses import VERSION_DRAFT, VERSION_PUBLISHED

MAX_STEPS = 50


# ---------- programs ----------

def create_program(
    session: Session,
    *,
    supervisor_id: int,
    title: str,
    description: str,
    capacity: int,
    enrollment_deadline: datetime,
) -> Program:
    program = Program(
        title=title,
        description=description,
        supervisor_id=supervisor_id,
        capacity=capacity,
        enrollment_deadline=_ensure_aware(enrollment_deadline),
    )
    session.add(program)
    session.flush()
    # Every new program starts with a draft (version 1).
    session.add(ProgramVersion(program_id=program.id, version_number=1, status=VERSION_DRAFT))
    session.commit()
    session.refresh(program)
    return program


def get_program_or_404(session: Session, program_id: int) -> Program:
    program = session.get(Program, program_id)
    if program is None:
        raise NotFoundError(f"program {program_id} not found")
    return program


def require_program_supervisor(session: Session, program: Program, user_id: int) -> None:
    if program.supervisor_id != user_id:
        raise ForbiddenError("only the program's supervisor may perform this action")


# ---------- versions ----------

def create_draft(session: Session, program_id: int, *, supervisor_id: int,
                 base_version_id: int | None = None) -> ProgramVersion:
    program = get_program_or_404(session, program_id)
    require_program_supervisor(session, program, supervisor_id)

    existing_draft = session.scalar(
        select(ProgramVersion).where(
            ProgramVersion.program_id == program_id,
            ProgramVersion.status == VERSION_DRAFT,
        )
    )
    if existing_draft is not None:
        raise ConflictError(f"draft version {existing_draft.id} already exists")

    next_number = (
        session.scalar(
            select(ProgramVersion.version_number)
            .where(ProgramVersion.program_id == program_id)
            .order_by(ProgramVersion.version_number.desc())
            .limit(1)
        )
        or 0
    ) + 1
    draft = ProgramVersion(
        program_id=program_id, version_number=next_number, status=VERSION_DRAFT
    )
    session.add(draft)
    session.flush()

    base = _resolve_base_for_copy(session, program, base_version_id)
    if base is not None:
        _copy_steps(session, source=base, target=draft)
    session.commit()
    session.refresh(draft)
    return draft


def _resolve_base_for_copy(
    session: Session, program: Program, base_version_id: int | None
) -> ProgramVersion | None:
    if base_version_id is None:
        # Default: start the draft from whatever learners currently see.
        if program.current_version_id is None:
            return None
        base_version_id = program.current_version_id
    base = session.get(ProgramVersion, base_version_id)
    if base is None or base.program_id != program.id:
        raise NotFoundError(f"base version {base_version_id} not found for this program")
    return base


def _copy_steps(session: Session, *, source: ProgramVersion, target: ProgramVersion) -> None:
    old_steps = source.steps
    old_id_to_new_id: dict[int, int] = {}
    for old in sorted(old_steps, key=lambda s: s.position):
        new = Step(
            version_id=target.id,
            position=old.position,
            title=old.title,
            instruction=old.instruction,
            pass_condition=old.pass_condition,
        )
        session.add(new)
        session.flush()
        old_id_to_new_id[old.id] = new.id
    for old in sorted(old_steps, key=lambda s: s.position):
        for prereq in old.prerequisites:
            session.add(
                StepPrerequisite(
                    step_id=old_id_to_new_id[old.id],
                    prerequisite_id=old_id_to_new_id[prereq.prerequisite_id],
                )
            )


def get_draft_or_404(session: Session, version_id: int) -> ProgramVersion:
    version = session.get(ProgramVersion, version_id)
    if version is None:
        raise NotFoundError(f"version {version_id} not found")
    if version.status != VERSION_DRAFT:
        raise ConflictError(
            f"version {version_id} is already published and is immutable"
        )
    return version


def replace_steps(session: Session, version_id: int, steps: list[StepIn], *,
                  supervisor_id: int) -> ProgramVersion:
    """Replace the full ordered step list of a draft and validate it.

    The whole set is swapped inside one flush: validation errors leave the
    draft untouched because the caller's transaction is rolled back.
    """
    version = get_draft_or_404(session, version_id)
    program = get_program_or_404(session, version.program_id)
    require_program_supervisor(session, program, supervisor_id)

    validate_step_payload(steps)

    # Wipe old step graph (prerequisites cascade through the ORM relationships).
    existing = session.scalars(select(Step).where(Step.version_id == version_id)).all()
    for step in existing:
        session.delete(step)
    session.flush()

    new_rows: list[Step] = []
    for index, payload in enumerate(steps, start=1):
        row = Step(
            version_id=version_id,
            position=index,
            title=payload.title,
            instruction=payload.instruction,
            pass_condition=payload.pass_condition,
        )
        session.add(row)
        new_rows.append(row)
    session.flush()

    for row, payload in zip(new_rows, steps):
        for prereq_position in set(payload.prerequisite_positions):
            session.add(
                StepPrerequisite(
                    step_id=row.id, prerequisite_id=new_rows[prereq_position - 1].id
                )
            )
    session.flush()
    session.commit()
    session.refresh(version)
    return version


def publish_version(session: Session, version_id: int, *, supervisor_id: int) -> ProgramVersion:
    version = get_draft_or_404(session, version_id)
    program = get_program_or_404(session, version.program_id)
    require_program_supervisor(session, program, supervisor_id)

    # Re-validate from persisted state: defence in depth at the freeze point.
    steps = sorted(version.steps, key=lambda s: s.position)
    validate_persisted_steps(steps)

    version.status = VERSION_PUBLISHED
    version.published_at = now_utc()
    program.current_version_id = version.id
    session.commit()
    session.refresh(version)
    return version


def rollback(session: Session, program_id: int, target_version_id: int, *,
             supervisor_id: int) -> Program:
    """Point new enrolments at an older published version.

    Existing enrolments keep their pinned version, so rollback never changes
    in-flight learning content.
    """
    program = get_program_or_404(session, program_id)
    require_program_supervisor(session, program, supervisor_id)
    target = session.get(ProgramVersion, target_version_id)
    if target is None or target.program_id != program_id:
        raise NotFoundError(f"version {target_version_id} not found for this program")
    if target.status != VERSION_PUBLISHED:
        raise ConflictError("only a published version can be rolled back to")
    program.current_version_id = target.id
    session.commit()
    return program


# ---------- validation ----------

def validate_step_payload(steps: list[StepIn]) -> None:
    if not 1 <= len(steps) <= MAX_STEPS:
        raise ValidationError(f"a version must contain between 1 and {MAX_STEPS} steps")

    n = len(steps)
    graph: dict[int, set[int]] = defaultdict(set)
    for index, payload in enumerate(steps, start=1):
        for prereq in payload.prerequisite_positions:
            if prereq == index:
                raise ValidationError(f"step {index} cannot be its own prerequisite")
            if not 1 <= prereq <= n:
                raise ValidationError(
                    f"step {index} references unknown prerequisite position {prereq}"
                )
            graph[index].add(prereq)
    _ensure_acyclic(graph, n)


def validate_persisted_steps(steps: list[Step]) -> None:
    if not 1 <= len(steps) <= MAX_STEPS:
        raise ValidationError(f"a version must contain between 1 and {MAX_STEPS} steps")
    positions = {step.position: step for step in steps}
    graph: dict[int, set[int]] = defaultdict(set)
    for step in steps:
        for prereq in step.prerequisites:
            prereq_step = next((s for s in steps if s.id == prereq.prerequisite_id), None)
            if prereq_step is None:
                raise ValidationError(
                    f"step {step.position} references unknown prerequisite step"
                )
            if prereq_step.position == step.position:
                raise ValidationError(
                    f"step {step.position} cannot be its own prerequisite"
                )
            graph[step.position].add(prereq_step.position)
    _ensure_acyclic(graph, len(steps))


def _ensure_acyclic(graph: dict[int, set[int]], n: int) -> None:
    """Iterative DFS cycle detection over 1..n nodes."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {i: WHITE for i in range(1, n + 1)}
    for start in range(1, n + 1):
        if color[start] != WHITE:
            continue
        stack: list[tuple[int, list[int]]] = [(start, sorted(graph.get(start, ())))]
        color[start] = GRAY
        while stack:
            node, neighbours = stack[-1]
            if neighbours:
                nxt = neighbours.pop()
                if color[nxt] == GRAY:
                    cycle = _describe_cycle(graph, nxt)
                    raise ValidationError(f"prerequisite cycle detected: {cycle}")
                if color[nxt] == WHITE:
                    color[nxt] = GRAY
                    stack.append((nxt, sorted(graph.get(nxt, ()))))
            else:
                color[node] = BLACK
                stack.pop()


def _describe_cycle(graph: dict[int, set[int]], entry: int) -> str:
    path = [entry]
    node = entry
    seen = {entry}
    while graph.get(node):
        node = sorted(graph[node])[0]
        if node in seen:
            path.append(node)
            return " -> ".join(str(p) for p in path)
        seen.add(node)
        path.append(node)
    return " -> ".join(str(p) for p in path)


def _ensure_aware(value: datetime) -> datetime:
    from datetime import timezone

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
