"""Program / version domain: ordered steps, prerequisite validation,
cycle detection, content digests and immutable publishing."""
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import clock as clock_svc
from app.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.models import (
    Program,
    ProgramVersion,
    Step,
    StepPrerequisite,
    VERSION_DRAFT,
    VERSION_PUBLISHED,
)

MAX_STEPS = 50


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_steps(steps: list[dict]) -> None:
    if not steps:
        raise ValidationError("A program version must contain at least one step.")
    if len(steps) > MAX_STEPS:
        raise ValidationError(f"A program version may contain at most {MAX_STEPS} steps.")

    keys: set[str] = set()
    positions: set[int] = set()
    for idx, step in enumerate(steps, start=1):
        key = (step.get("key") or "").strip()
        if not key:
            raise ValidationError(f"Step #{idx} is missing a key.")
        if key in keys:
            raise ValidationError(f"Duplicate step key: {key!r}.")
        keys.add(key)

        position = step.get("position")
        if not isinstance(position, int) or position < 1:
            raise ValidationError(f"Step {key!r} must have integer position >= 1.")
        if position in positions:
            raise ValidationError(f"Duplicate step position: {position}.")
        positions.add(position)

        if not (step.get("instruction") or "").strip():
            raise ValidationError(f"Step {key!r} is missing an instruction.")
        if not (step.get("pass_condition") or "").strip():
            raise ValidationError(f"Step {key!r} is missing a pass condition.")

    # Positions must be an exact 1..N ordering.
    if positions != set(range(1, len(steps) + 1)):
        raise ValidationError(
            "Step positions must be consecutive integers starting at 1."
        )

    by_key = {s["key"].strip(): s for s in steps}
    for step in steps:
        key = step["key"].strip()
        prereqs = step.get("prerequisite_keys", []) or []
        if len(prereqs) != len(set(prereqs)):
            raise ValidationError(f"Step {key!r} lists a duplicate prerequisite.")
        for prereq in prereqs:
            prereq = prereq.strip()
            if prereq not in by_key:
                raise ValidationError(
                    f"Step {key!r} depends on unknown step {prereq!r}."
                )
            if prereq == key:
                raise ValidationError(f"Step {key!r} cannot depend on itself.")
            # A prerequisite is an earlier ordered step.
            if by_key[prereq]["position"] >= step["position"]:
                raise ValidationError(
                    f"Step {key!r} (position {step['position']}) can only depend on "
                    f"earlier steps; {prereq!r} is position {by_key[prereq]['position']}."
                )

    _assert_acyclic(by_key)


def _assert_acyclic(by_key: dict[str, dict]) -> None:
    """Kahn's algorithm over prerequisite edges prereq -> step."""
    indegree: dict[str, int] = {k: 0 for k in by_key}
    adjacency: dict[str, list[str]] = {k: [] for k in by_key}
    for key, step in by_key.items():
        for prereq in step.get("prerequisite_keys", []) or []:
            adjacency[prereq.strip()].append(key)
            indegree[key] += 1

    queue = [k for k, deg in indegree.items() if deg == 0]
    seen = 0
    while queue:
        node = queue.pop()
        seen += 1
        for nxt in adjacency[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)

    if seen != len(by_key):
        cycle_keys = [k for k, deg in indegree.items() if deg > 0]
        raise ValidationError(
            "Step dependencies contain a cycle involving: "
            + ", ".join(sorted(cycle_keys))
        )


def compute_content_digest(steps: list[dict]) -> str:
    """SHA-256 over the canonical structure of the version:
    ordered steps with keys, instructions, pass conditions and edges."""
    canonical = [
        {
            "position": s["position"],
            "key": s["key"].strip(),
            "instruction": s["instruction"].strip(),
            "pass_condition": s["pass_condition"].strip(),
            "prerequisite_keys": sorted(p.strip() for p in (s.get("prerequisite_keys") or [])),
        }
        for s in sorted(steps, key=lambda s: s["position"])
    ]
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_program(db: Session, *, supervisor_id: int, title: str, description: str = "") -> Program:
    program = Program(
        title=title,
        description=description or "",
        owner_id=supervisor_id,
        current_version_id=None,
    )
    db.add(program)
    db.flush()
    return program


def _get_program(db: Session, program_id: int, supervisor_id: int | None = None) -> Program:
    program = db.get(Program, program_id)
    if program is None:
        raise NotFoundError(f"Program {program_id} not found.")
    if supervisor_id is not None and program.owner_id != supervisor_id:
        raise ForbiddenError("Only the owning supervisor can modify this program.")
    return program


def create_version(
    db: Session,
    *,
    supervisor_id: int,
    program_id: int,
    steps: list[dict],
    publish: bool,
) -> ProgramVersion:
    """Create a new version. Modifications always produce a NEW version;
    rows of published versions are never altered afterwards."""
    program = _get_program(db, program_id, supervisor_id)

    normalized = [
        {
            "key": s["key"].strip(),
            "position": s["position"],
            "instruction": s["instruction"].strip(),
            "pass_condition": s["pass_condition"].strip(),
            "prerequisite_keys": [p.strip() for p in (s.get("prerequisite_keys") or [])],
        }
        for s in steps
    ]
    _validate_steps(normalized)
    digest = compute_content_digest(normalized)

    last_number = db.scalar(
        select(ProgramVersion.version_number)
        .where(ProgramVersion.program_id == program_id)
        .order_by(ProgramVersion.version_number.desc())
        .limit(1)
    )
    version = ProgramVersion(
        program_id=program_id,
        version_number=(last_number or 0) + 1,
        status=VERSION_DRAFT,
        content_digest=None,
    )
    db.add(version)
    db.flush()

    for s in normalized:
        step = Step(
            version_id=version.id,
            key=s["key"],
            position=s["position"],
            instruction=s["instruction"],
            pass_condition=s["pass_condition"],
        )
        db.add(step)
        db.flush()
        for prereq_key in s["prerequisite_keys"]:
            db.add(
                StepPrerequisite(step_id=step.id, prerequisite_key=prereq_key)
            )

    if publish:
        _publish(db, program=program, version=version, digest=digest)
    db.flush()
    return version


def _publish(
    db: Session, *, program: Program, version: ProgramVersion, digest: str
) -> None:
    # Re-validate the actual persisted graph defensively.
    steps = _load_version_steps(db, version.id)
    _validate_steps(steps)

    version.status = VERSION_PUBLISHED
    version.content_digest = digest
    version.published_at = clock_svc.now(db).replace(tzinfo=None)
    # Publishing makes this version the current rollout target.
    program.current_version_id = version.id
    db.flush()


def publish_version(db: Session, *, supervisor_id: int, version_id: int) -> ProgramVersion:
    version = db.get(ProgramVersion, version_id)
    if version is None:
        raise NotFoundError(f"Version {version_id} not found.")
    program = _get_program(db, version.program_id, supervisor_id)
    if version.status == VERSION_PUBLISHED:
        raise ConflictError(
            f"Version {version.version_number} is already published and immutable."
        )
    steps = _load_version_steps(db, version.id)
    digest = compute_content_digest(steps)
    _publish(db, program=program, version=version, digest=digest)
    return version


def rollback(db: Session, *, supervisor_id: int, program_id: int, version_id: int) -> Program:
    """Point the program at a previously published version. Already-started
    enrolments stay pinned to their own version_id."""
    program = _get_program(db, program_id, supervisor_id)
    target = db.get(ProgramVersion, version_id)
    if target is None or target.program_id != program_id:
        raise NotFoundError("Target version not found for this program.")
    if target.status != VERSION_PUBLISHED:
        raise ValidationError("Only published versions can be rollback targets.")
    program.current_version_id = target.id
    db.flush()
    return program


def _load_version_steps(db: Session, version_id: int) -> list[dict]:
    steps = db.scalars(
        select(Step)
        .where(Step.version_id == version_id)
        .order_by(Step.position)
        .options(selectinload(Step.prerequisites))
    ).all()
    return [s.to_dict() for s in steps]


def get_version(db: Session, version_id: int) -> ProgramVersion:
    version = db.scalar(
        select(ProgramVersion)
        .where(ProgramVersion.id == version_id)
        .options(selectinload(ProgramVersion.steps).selectinload(Step.prerequisites))
    )
    if version is None:
        raise NotFoundError(f"Version {version_id} not found.")
    return version


def list_versions(db: Session, program_id: int) -> list[ProgramVersion]:
    _get_program(db, program_id)
    return list(
        db.scalars(
            select(ProgramVersion)
            .where(ProgramVersion.program_id == program_id)
            .order_by(ProgramVersion.version_number)
        ).all()
    )
