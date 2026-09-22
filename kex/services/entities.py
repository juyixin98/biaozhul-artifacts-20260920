"""Entity read/export service.

All queries are workspace-scoped and joined to a live ``workspace_documents``
row, so deleted documents can never surface through entities or exports.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Document,
    Entity,
    WorkspaceDocument,
    WorkspaceState,
)
from .workspaces import WorkspaceError


def _resolve_rule_version_id(
    db: Session, workspace_id: int, rule_version: int | None
) -> int:
    state = db.get(WorkspaceState, workspace_id)
    if rule_version is None:
        if state.active_rule_version_id is None:
            raise WorkspaceError("workspace has no rule version", 409)
        return state.active_rule_version_id
    from ..models import RuleVersion

    rv = db.scalar(
        select(RuleVersion).where(
            RuleVersion.workspace_id == workspace_id,
            RuleVersion.version == rule_version,
        )
    )
    if rv is None:
        raise WorkspaceError(f"rule version {rule_version} not found", 404)
    return rv.id


def list_entities(
    db: Session,
    workspace_id: int,
    *,
    ws_doc_id: int | None = None,
    entity_type: str | None = None,
    rule_version: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[Entity], int]:
    rv_id = _resolve_rule_version_id(db, workspace_id, rule_version)
    q = (
        select(Entity)
        .join(WorkspaceDocument, WorkspaceDocument.id == Entity.workspace_document_id)
        .where(
            Entity.workspace_id == workspace_id,
            Entity.rule_version_id == rv_id,
        )
    )
    if ws_doc_id is not None:
        q = q.where(Entity.workspace_document_id == ws_doc_id)
    if entity_type is not None:
        q = q.where(Entity.entity_type == entity_type.upper())

    rows = list(
        db.scalars(
            q.order_by(Entity.workspace_document_id, Entity.start_char)
            .limit(limit)
            .offset(offset)
        ).all()
    )
    # Count under the same filters (without the join safety relaxed).
    count_q = (
        select(Entity.id)
        .join(WorkspaceDocument, WorkspaceDocument.id == Entity.workspace_document_id)
        .where(
            Entity.workspace_id == workspace_id,
            Entity.rule_version_id == rv_id,
        )
    )
    if ws_doc_id is not None:
        count_q = count_q.where(Entity.workspace_document_id == ws_doc_id)
    if entity_type is not None:
        count_q = count_q.where(Entity.entity_type == entity_type.upper())
    total = len(db.execute(count_q).all())
    return rows, total


def group_canonical(
    db: Session, workspace_id: int, *, rule_version: int | None = None
) -> dict[str, list[dict]]:
    """Canonical-name groups preserving every original surface mention."""
    rv_id = _resolve_rule_version_id(db, workspace_id, rule_version)
    rows = db.scalars(
        select(Entity)
        .join(WorkspaceDocument, WorkspaceDocument.id == Entity.workspace_document_id)
        .where(
            Entity.workspace_id == workspace_id,
            Entity.rule_version_id == rv_id,
        )
        .order_by(Entity.entity_type, Entity.canonical_name, Entity.start_char)
    ).all()
    grouped: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for e in rows:
        grouped[e.entity_type][e.canonical_name].append(
            {
                "text": e.text,  # original mention kept verbatim
                "document_id": e.workspace_document_id,
                "start_char": e.start_char,
                "end_char": e.end_char,
                "matched_rule": e.matched_rule,
            }
        )
    return {etype: dict(canon) for etype, canon in grouped.items()}


def export_workspace(
    db: Session, workspace_id: int, *, rule_version: int | None = None
) -> list[dict]:
    """Full extraction export under one (default: active) rule version.

    One JSON object per surviving workspace document. Deleted documents are
    absent by construction (inner join), so exports cannot leak them.
    """
    rv_id = _resolve_rule_version_id(db, workspace_id, rule_version)
    docs = db.scalars(
        select(WorkspaceDocument)
        .where(WorkspaceDocument.workspace_id == workspace_id)
        .order_by(WorkspaceDocument.id)
    ).all()
    out: list[dict] = []
    for ws_doc in docs:
        doc = db.get(Document, ws_doc.document_id)
        entities = db.scalars(
            select(Entity)
            .where(
                Entity.workspace_id == workspace_id,
                Entity.workspace_document_id == ws_doc.id,
                Entity.rule_version_id == rv_id,
            )
            .order_by(Entity.start_char)
        ).all()
        out.append(
            {
                "document_id": ws_doc.id,
                "title": ws_doc.title,
                "sha256": doc.sha256,
                "rule_version_id": rv_id,
                "entities": [
                    {
                        "entity_type": e.entity_type,
                        "text": e.text,
                        "canonical_name": e.canonical_name,
                        "start_char": e.start_char,
                        "end_char": e.end_char,
                        "matched_rule": e.matched_rule,
                    }
                    for e in entities
                ],
            }
        )
    return out
