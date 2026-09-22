"""Per-workspace authentication helpers."""
from __future__ import annotations

from flask import g, request

from ..db import get_sessionmaker
from ..models import Workspace
from ..services.workspaces import WorkspaceError, authenticate


def load_workspace() -> Workspace:
    """Resolve and authenticate the workspace for the current request.

    The workspace id is in the URL (``/api/workspaces/<ws_id>/...``); the
    shared secret must be supplied as ``X-Workspace-Key``.
    """
    if "workspace" in g:
        return g.workspace  # type: ignore[no-any-return]

    ws_id = request.view_args.get("workspace_id") if request.view_args else None
    try:
        ws_id = int(ws_id)
    except (TypeError, ValueError):
        raise WorkspaceError("workspace not found", 404)

    api_key = request.headers.get("X-Workspace-Key")
    SessionLocal = get_sessionmaker()
    db = SessionLocal()
    g.db = db
    g.workspace = authenticate(db, ws_id, api_key)
    return g.workspace


def get_db():
    """Request-scoped session (created lazily together with auth)."""
    if "db" not in g:
        SessionLocal = get_sessionmaker()
        g.db = SessionLocal()
    return g.db


def close_db(_exc=None) -> None:  # noqa: ANN001
    db = g.pop("db", None)
    if db is not None:
        db.close()
