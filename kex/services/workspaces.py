"""Workspace bootstrap and authentication."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..extraction import builtin_snapshot, compile_snapshot, serialize_snapshot
from ..models import (
    IndexGeneration,
    RuleVersion,
    Workspace,
    WorkspaceState,
)


class WorkspaceError(Exception):
    """4xx-level service error with an HTTP-ish status code."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def create_workspace(db: Session, name: str, *, seed_builtin: bool = True) -> Workspace:
    name = (name or "").strip()
    if not name:
        raise WorkspaceError("workspace name is required")
    if db.scalar(select(Workspace).where(Workspace.name == name)) is not None:
        raise WorkspaceError(f"workspace {name!r} already exists", 409)

    ws = Workspace(name=name, api_key=secrets.token_urlsafe(32))
    db.add(ws)
    db.flush()
    state = WorkspaceState(
        workspace_id=ws.id,
        next_rule_version=1,
        next_index_generation=1,
        active_rule_version_id=None,
        active_index_generation_id=None,
    )
    db.add(state)
    db.flush()

    if seed_builtin:
        # Publish the immutable built-in v1 dictionary.
        snapshot_json = serialize_snapshot(builtin_snapshot())
        checksum = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        v1 = RuleVersion(
            workspace_id=ws.id,
            version=1,
            snapshot=snapshot_json,
            checksum=checksum,
            note="builtin dictionary",
            status="published",
        )
        db.add(v1)
        db.flush()
        # The empty generation 1 is immediately active and complete, so
        # search reads a consistent (empty) index from the very beginning;
        # uploaded documents are indexed into it incrementally.
        gen1 = IndexGeneration(
            workspace_id=ws.id,
            generation=1,
            rule_version_id=v1.id,
            status="active",
        )
        db.add(gen1)
        db.flush()
        state.next_rule_version = 2
        state.next_index_generation = 2
        state.active_rule_version_id = v1.id
        state.active_index_generation_id = gen1.id
        db.flush()
        ws.state = state
    db.commit()
    return ws


def authenticate(db: Session, workspace_id: int, api_key: str | None) -> Workspace:
    ws = db.get(Workspace, workspace_id)
    if ws is None:
        raise WorkspaceError("workspace not found", 404)
    if not api_key or not hmac.compare_digest(ws.api_key, api_key):
        raise WorkspaceError("invalid workspace credentials", 401)
    return ws


def get_active_rule(db: Session, ws_id: int) -> RuleVersion:
    rv = db.get(RuleVersion, _require_state(db, ws_id).active_rule_version_id)
    if rv is None:
        raise WorkspaceError("workspace has no active rule version", 409)
    return rv


def _require_state(db: Session, ws_id: int) -> WorkspaceState:
    state = db.get(WorkspaceState, ws_id)
    if state is None:
        raise WorkspaceError("workspace not found", 404)
    return state


def compiled_active_rules(db: Session, ws_id: int):
    return compile_snapshot(get_active_rule(db).snapshot)
