"""Document upload (content-addressed, workspace-isolated) and deletion."""
from __future__ import annotations

import hashlib

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Document,
    Entity,
    IndexGeneration,
    JobItem,
    WorkspaceDocument,
    WorkspaceState,
)
from ..services import jobs as jobs_service
from ..services import search_index
from .workspaces import WorkspaceError, get_active_rule


def _decode_utf8(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceError(f"only UTF-8 text is accepted: {exc}", 400) from exc


def upload_document(
    db: Session,
    workspace_id: int,
    *,
    raw: bytes | None = None,
    text: str | None = None,
    title: str | None = None,
) -> tuple[WorkspaceDocument, bool, int]:
    """Create or reuse a content-addressed document inside this workspace.

    Returns ``(ws_document, deduplicated, job_id)``. The global blob may be
    shared with another workspace that happens to hold identical bytes, but
    every read path is scoped through ``workspace_documents`` so access can
    never leak between workspaces.
    """
    if raw is not None:
        content = _decode_utf8(raw)
    elif text is not None:
        if not isinstance(text, str):
            raise WorkspaceError("text must be a string", 400)
        content = text
    else:
        raise WorkspaceError("raw bytes or text required", 400)
    if not content:
        raise WorkspaceError("document content is empty", 400)

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    doc = db.scalar(select(Document).where(Document.sha256 == digest))
    blob_reused = doc is not None
    if doc is None:
        doc = Document(sha256=digest, content=content, length=len(content))
        db.add(doc)
        db.flush()

    existing = db.scalar(
        select(WorkspaceDocument).where(
            WorkspaceDocument.workspace_id == workspace_id,
            WorkspaceDocument.document_id == doc.id,
        )
    )
    if existing is not None:
        # Same workspace already holds this content: reuse, no new job.
        return existing, True, -1

    rule = get_active_rule(db, workspace_id)
    state = db.get(WorkspaceState, workspace_id)
    title = (title or f"doc-{digest[:12]}").strip()[:512] or f"doc-{digest[:12]}"
    ws_doc = WorkspaceDocument(
        workspace_id=workspace_id, document_id=doc.id, title=title
    )
    db.add(ws_doc)
    db.flush()

    # Schedule incremental extraction against the active rules and indexing
    # into the active generation plus EVERY in-flight rebuild generation, so
    # a rebuild running concurrently with this upload cannot miss the
    # document (even if several rebuilds happen to be in flight).
    building_gens = db.scalars(
        select(IndexGeneration).where(
            IndexGeneration.workspace_id == workspace_id,
            IndexGeneration.status == "building",
        )
    ).all()
    job = jobs_service.create_incremental_job(
        db,
        workspace_id=workspace_id,
        rule_version_id=rule.id,
        ws_docs=[ws_doc],
        active_generation_id=state.active_index_generation_id,
        building_generation_ids=[g.id for g in building_gens],
        digest=digest,
    )
    db.commit()
    return ws_doc, blob_reused, job.id


def list_documents(db: Session, workspace_id: int, *, limit: int = 50, offset: int = 0):
    total = db.scalar(
        select(func.count(WorkspaceDocument.id)).where(
            WorkspaceDocument.workspace_id == workspace_id
        )
    )
    rows = db.scalars(
        select(WorkspaceDocument)
        .where(WorkspaceDocument.workspace_id == workspace_id)
        .order_by(WorkspaceDocument.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return rows, total or 0


def get_workspace_document(
    db: Session, workspace_id: int, ws_doc_id: int
) -> WorkspaceDocument:
    ws_doc = db.get(WorkspaceDocument, ws_doc_id)
    if ws_doc is None or ws_doc.workspace_id != workspace_id:
        # Do not distinguish "not found" from "forbidden": both are 404.
        raise WorkspaceError("document not found", 404)
    return ws_doc


def delete_document(db: Session, workspace_id: int, ws_doc_id: int) -> None:
    """Delete a document and purge every trace it left, in one transaction.

    1. verify ownership;
    2. remove its rows from EVERY index generation of this workspace, so
       neither active nor building/retired generations can leak text;
    3. remove entities and queued job items explicitly (FK CASCADE also
       covers them, but belt-and-braces for the delete-race window where an
       item is currently leased);
    4. remove the workspace link;
    5. delete the global content blob only when no workspace references it.
    """
    ws_doc = get_workspace_document(db, workspace_id, ws_doc_id)
    doc_id = ws_doc.document_id

    gen_ids = [
        row[0]
        for row in db.execute(
            select(IndexGeneration.id).where(
                IndexGeneration.workspace_id == workspace_id
            )
        ).all()
    ]
    for gen_id in gen_ids:
        search_index.remove_document(db, gen_id, ws_doc_id)

    # Explicit purge: a leased worker re-checks WorkspaceDocument existence
    # before writing, and these deletes guarantee no row survives anyway.
    db.query(Entity).filter(Entity.workspace_document_id == ws_doc_id).delete(
        synchronize_session=False
    )
    db.query(JobItem).filter(JobItem.workspace_document_id == ws_doc_id).delete(
        synchronize_session=False
    )

    db.delete(ws_doc)
    db.flush()

    remaining = db.scalar(
        select(func.count(WorkspaceDocument.id)).where(
            WorkspaceDocument.document_id == doc_id
        )
    )
    if not remaining:
        blob = db.get(Document, doc_id)
        if blob is not None:
            db.delete(blob)
    db.commit()
