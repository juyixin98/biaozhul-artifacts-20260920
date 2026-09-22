"""Rule publishing (immutable), rebuild triggering and rollback."""
from __future__ import annotations

import datetime as _dt
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..extraction import (
    RuleValidationError,
    canonicalize_snapshot,
    compile_snapshot,
    serialize_snapshot,
)
from ..models import ExtractionJob, IndexGeneration, RuleVersion, WorkspaceState
from . import jobs as jobs_service
from .workspaces import WorkspaceError


def list_versions(db: Session, workspace_id: int) -> list[RuleVersion]:
    return list(
        db.scalars(
            select(RuleVersion)
            .where(RuleVersion.workspace_id == workspace_id)
            .order_by(RuleVersion.version)
        ).all()
    )


def publish_rules(
    db: Session,
    workspace_id: int,
    raw_rules: dict,
    *,
    note: str = "",
) -> tuple[RuleVersion, IndexGeneration | None, ExtractionJob | None]:
    """Validate + freeze a new immutable rule version and rebuild for it.

    * Republishing the *same* rule content is idempotent: the existing
      version is returned and no rebuild starts.
    * Otherwise a new row is inserted, immediately the active rule pointer
      moves to it (uploads use the new dictionary), and a rebuild builds a
      fresh index generation. Search keeps serving the previous complete
      generation until the rebuild succeeds; the pointer then swaps once.
    """
    try:
        snapshot = canonicalize_snapshot(raw_rules)
    except RuleValidationError as exc:
        raise WorkspaceError(str(exc), 400) from exc
    # Compile once to surface any regex/compiler error eagerly.
    compile_snapshot(snapshot)
    snapshot_json = serialize_snapshot(snapshot)
    checksum = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()

    state = db.get(WorkspaceState, workspace_id)
    same = db.scalar(
        select(RuleVersion).where(
            RuleVersion.workspace_id == workspace_id,
            RuleVersion.checksum == checksum,
        )
    )
    if same is not None:
        db.commit()
        return same, None, None

    rv = RuleVersion(
        workspace_id=workspace_id,
        version=state.next_rule_version,
        snapshot=snapshot_json,
        checksum=checksum,
        note=(note or "")[:512],
        status="published",
    )
    db.add(rv)
    db.flush()
    state.next_rule_version += 1
    state.active_rule_version_id = rv.id

    job, gen = jobs_service.create_rebuild_job(db, workspace_id, rv.id)
    db.commit()
    return rv, gen, job


def rollback_rules(
    db: Session, workspace_id: int, target_version: int
) -> tuple[RuleVersion, IndexGeneration, ExtractionJob | None]:
    """Switch to a complete older generation, or rebuild the old rules.

    Historical evidence is never modified: old entity rows and old retired
    generations are preserved. Rollback only moves pointers or schedules a
    fresh generation bound to the chosen immutable rule version.
    """
    state = db.get(WorkspaceState, workspace_id)
    rv = db.scalar(
        select(RuleVersion).where(
            RuleVersion.workspace_id == workspace_id,
            RuleVersion.version == target_version,
        )
    )
    if rv is None:
        raise WorkspaceError(f"rule version {target_version} not found", 404)

    # Prefer switching to an already-complete (active/retired) generation
    # that was built against exactly these rules.
    complete_gen = db.scalar(
        select(IndexGeneration)
        .where(
            IndexGeneration.workspace_id == workspace_id,
            IndexGeneration.rule_version_id == rv.id,
            IndexGeneration.status.in_(["active", "retired"]),
        )
        .order_by(IndexGeneration.generation.desc())
    )
    if complete_gen is not None:
        current_id = state.active_index_generation_id
        if current_id is not None and current_id != complete_gen.id:
            current = db.get(IndexGeneration, current_id)
            if current is not None and current.status == "active":
                current.status = "retired"
        complete_gen.status = "active"
        complete_gen.activated_at = _dt.datetime.now(_dt.timezone.utc)
        state.active_index_generation_id = complete_gen.id
        state.active_rule_version_id = rv.id
        db.commit()
        return rv, complete_gen, None

    # No complete generation exists for those rules: build a fresh one.
    state.active_rule_version_id = rv.id
    job, gen = jobs_service.create_rebuild_job(db, workspace_id, rv.id)
    db.commit()
    return rv, gen, job
