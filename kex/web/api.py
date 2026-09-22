"""HTTP API.

All workspace routes require ``X-Workspace-Key``. Request/response bodies
are UTF-8 JSON except document upload, which accepts either JSON
``{"text": ...}`` or a raw ``text/plain`` body.
"""
from __future__ import annotations

from flask import Blueprint, Response, jsonify, request
from sqlalchemy import select

from ..models import (
    Document,
    ExtractionJob,
    IndexGeneration,
    JobItem,
    RuleVersion,
    WorkspaceState,
)
from ..services import documents as docs_service
from ..services import entities as entities_service
from ..services import jobs as jobs_service
from ..services import rules as rules_service
from ..services import search_index
from ..services.workspaces import WorkspaceError, create_workspace
from .auth import close_db, get_db, load_workspace

api_bp = Blueprint("api", __name__)


# --------------------------------------------------------------------------- #
# teardown
# --------------------------------------------------------------------------- #
@api_bp.teardown_request
def _teardown(exc):  # noqa: ANN001
    close_db(exc)


def _pagination(default_limit: int = 50, maximum: int = 200):
    try:
        limit = int(request.args.get("limit", default_limit))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        raise WorkspaceError("limit/offset must be integers")
    return max(1, min(limit, maximum)), max(0, offset)


def _rule_version_number(rv: RuleVersion) -> int:
    return rv.version


# --------------------------------------------------------------------------- #
# Workspace bootstrap (management endpoint; returns the one-time API key)
# --------------------------------------------------------------------------- #
@api_bp.post("/api/workspaces")
def api_create_workspace():
    body = request.get_json(silent=True) or {}
    name = body.get("name")
    db = get_db()
    ws = create_workspace(db, name)
    return (
        jsonify(
            {
                "workspace_id": ws.id,
                "name": ws.name,
                "api_key": ws.api_key,
                "warning": "save this key now; it is never returned again",
            }
        ),
        201,
    )


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #
@api_bp.post("/api/workspaces/<int:workspace_id>/documents")
def api_upload_document(workspace_id: int):
    ws = load_workspace()
    db = get_db()
    title = request.args.get("title")
    content_type = (request.content_type or "").split(";")[0].strip()
    if content_type == "application/json":
        body = request.get_json(silent=True) or {}
        text = body.get("text")
        title = body.get("title", title)
        if text is None:
            raise WorkspaceError("JSON body requires 'text'")
        ws_doc, dedup, job_id = docs_service.upload_document(
            db, ws.id, text=text, title=title
        )
    elif content_type in ("text/plain", "application/octet-stream", ""):
        raw = request.get_data()
        ws_doc, dedup, job_id = docs_service.upload_document(
            db, ws.id, raw=raw, title=title
        )
    else:
        raise WorkspaceError(f"unsupported content type: {content_type}")

    return (
        jsonify(
            {
                "document_id": ws_doc.id,
                "title": ws_doc.title,
                "deduplicated": dedup,
                "extraction_job_id": job_id,
                "status_url": f"/api/workspaces/{ws.id}/jobs/{job_id}",
            }
        ),
        200 if dedup else 202,
    )


@api_bp.get("/api/workspaces/<int:workspace_id>/documents")
def api_list_documents(workspace_id: int):
    ws = load_workspace()
    db = get_db()
    limit, offset = _pagination()
    rows, total = docs_service.list_documents(db, ws.id, limit=limit, offset=offset)
    return jsonify(
        {
            "total": total,
            "limit": limit,
            "offset": offset,
            "documents": [
                {
                    "document_id": d.id,
                    "title": d.title,
                    "sha256": db.get(Document, d.document_id).sha256,
                }
                for d in rows
            ],
        }
    )


@api_bp.get("/api/workspaces/<int:workspace_id>/documents/<int:doc_id>")
def api_get_document(workspace_id: int, doc_id: int):
    ws = load_workspace()
    db = get_db()
    ws_doc = docs_service.get_workspace_document(db, ws.id, doc_id)
    from ..models import Document

    doc = db.get(Document, ws_doc.document_id)
    return jsonify(
        {
            "document_id": ws_doc.id,
            "title": ws_doc.title,
            "sha256": doc.sha256,
            "length": doc.length,
            "content": doc.content,
        }
    )


@api_bp.delete("/api/workspaces/<int:workspace_id>/documents/<int:doc_id>")
def api_delete_document(workspace_id: int, doc_id: int):
    ws = load_workspace()
    db = get_db()
    docs_service.delete_document(db, ws.id, doc_id)
    return jsonify({"deleted": doc_id})


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
@api_bp.get("/api/workspaces/<int:workspace_id>/rules")
def api_list_rules(workspace_id: int):
    ws = load_workspace()
    db = get_db()
    state = db.get(WorkspaceState, ws.id)
    versions = rules_service.list_versions(db, ws.id)
    active_id = state.active_rule_version_id
    return jsonify(
        {
            "active_version": next(
                (v.version for v in versions if v.id == active_id), None
            ),
            "versions": [
                {
                    "version": v.version,
                    "rule_version_id": v.id,
                    "checksum": v.checksum,
                    "note": v.note,
                    "status": v.status,
                    "created_at": v.created_at.isoformat(),
                    "active": v.id == active_id,
                }
                for v in versions
            ],
        }
    )


@api_bp.post("/api/workspaces/<int:workspace_id>/rules/publish")
def api_publish_rules(workspace_id: int):
    ws = load_workspace()
    db = get_db()
    body = request.get_json(silent=True)
    if body is None:
        raise WorkspaceError("JSON body required")
    rules = body.get("rules", body)
    rv, gen, job = rules_service.publish_rules(
        db, ws.id, rules, note=body.get("note", "")
    )
    response = {
        "version": rv.version,
        "rule_version_id": rv.id,
        "checksum": rv.checksum,
        "immutable": True,
    }
    if job is None:
        response["rebuild"] = "unchanged: version already existed"
    else:
        response["rebuild"] = {
            "job_id": job.id,
            "index_generation": gen.generation,
            "status": "building; active index remains the previous complete generation",
        }
    return jsonify(response), 200 if job is None else 202


@api_bp.post("/api/workspaces/<int:workspace_id>/rules/rollback")
def api_rollback_rules(workspace_id: int):
    ws = load_workspace()
    db = get_db()
    body = request.get_json(silent=True) or {}
    target = body.get("version")
    if not isinstance(target, int):
        raise WorkspaceError("'version' (integer) required")
    rv, gen, job = rules_service.rollback_rules(db, ws.id, target)
    response = {
        "active_version": rv.version,
        "active_index_generation": gen.generation,
        "history_rewritten": False,
    }
    if job is not None:
        response["rebuild"] = {"job_id": job.id, "index_generation": gen.generation}
    return jsonify(response), 202 if job is not None else 200


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
def _job_dict(db, job: ExtractionJob) -> dict:
    items = db.scalars(select(JobItem).where(JobItem.job_id == job.id)).all()
    counts: dict[str, int] = {}
    for it in items:
        counts[it.status] = counts.get(it.status, 0) + 1
    failed_detail = [
        {
            "item_id": it.id,
            "document_id": it.workspace_document_id,
            "stage": it.stage,
            "error": it.last_error,
            "attempts": it.attempts,
            "document_sha256": it.document_sha256,
        }
        for it in items
        if it.status == "failed"
    ]
    return {
        "job_id": job.id,
        "kind": job.kind,
        "status": job.status,
        "rule_version_id": job.target_rule_version_id,
        "index_generation_id": job.target_index_generation_id,
        "created_at": job.created_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error": job.error,
        "items": {"total": len(items), **counts},
        "failed_items": failed_detail,
    }


@api_bp.get("/api/workspaces/<int:workspace_id>/jobs")
def api_list_jobs(workspace_id: int):
    ws = load_workspace()
    db = get_db()
    jobs = db.scalars(
        select(ExtractionJob)
        .where(ExtractionJob.workspace_id == ws.id)
        .order_by(ExtractionJob.id.desc())
        .limit(100)
    ).all()
    return jsonify({"jobs": [_job_dict(db, j) for j in jobs]})


@api_bp.get("/api/workspaces/<int:workspace_id>/jobs/<int:job_id>")
def api_get_job(workspace_id: int, job_id: int):
    ws = load_workspace()
    db = get_db()
    job = db.get(ExtractionJob, job_id)
    if job is None or job.workspace_id != ws.id:
        raise WorkspaceError("job not found", 404)
    return jsonify(_job_dict(db, job))


@api_bp.post("/api/workspaces/<int:workspace_id>/jobs/<int:job_id>/retry")
def api_retry_job(workspace_id: int, job_id: int):
    ws = load_workspace()
    db = get_db()
    job = db.get(ExtractionJob, job_id)
    if job is None or job.workspace_id != ws.id:
        raise WorkspaceError("job not found", 404)
    try:
        jobs_service.requeue_failed(db, ws.id, job_id)
        db.commit()
    except LookupError as exc:
        raise WorkspaceError(str(exc), 404) from exc
    return jsonify(_job_dict(db, job))


# --------------------------------------------------------------------------- #
# Entities & export
# --------------------------------------------------------------------------- #
@api_bp.get("/api/workspaces/<int:workspace_id>/entities")
def api_list_entities(workspace_id: int):
    ws = load_workspace()
    db = get_db()
    try:
        ws_doc_id = request.args.get("document_id", type=int)
        entity_type = request.args.get("type")
        rule_version = request.args.get("rule_version", type=int)
        limit, offset = _pagination(default_limit=100)
        rows, total = entities_service.list_entities(
            db,
            ws.id,
            ws_doc_id=ws_doc_id,
            entity_type=entity_type,
            rule_version=rule_version,
            limit=limit,
            offset=offset,
        )
    except WorkspaceError:
        raise
    return jsonify(
        {
            "total": total,
            "rule_version": rule_version,
            "entities": [
                {
                    "entity_id": e.id,
                    "document_id": e.workspace_document_id,
                    "entity_type": e.entity_type,
                    "text": e.text,
                    "canonical_name": e.canonical_name,
                    "start_char": e.start_char,
                    "end_char": e.end_char,
                    "matched_rule": e.matched_rule,
                }
                for e in rows
            ],
        }
    )


@api_bp.get("/api/workspaces/<int:workspace_id>/entities/grouped")
def api_grouped_entities(workspace_id: int):
    ws = load_workspace()
    db = get_db()
    rule_version = request.args.get("rule_version", type=int)
    return jsonify(
        {
            "rule_version": rule_version,
            "groups": entities_service.group_canonical(
                db, ws.id, rule_version=rule_version
            ),
        }
    )


@api_bp.get("/api/workspaces/<int:workspace_id>/export")
def api_export(workspace_id: int):
    ws = load_workspace()
    db = get_db()
    rule_version = request.args.get("rule_version", type=int)
    records = entities_service.export_workspace(db, ws.id, rule_version=rule_version)
    import json

    payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
    return Response(
        payload,
        mimetype="application/x-ndjson; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="workspace-{ws.id}-export.ndjson"'},
    )


# --------------------------------------------------------------------------- #
# Search & keywords
# --------------------------------------------------------------------------- #
def _active_generation(db, ws_id: int) -> IndexGeneration:
    state = db.get(WorkspaceState, ws_id)
    gen = (
        db.get(IndexGeneration, state.active_index_generation_id)
        if state and state.active_index_generation_id
        else None
    )
    if gen is None:
        raise WorkspaceError("search index not ready", 503)
    return gen


@api_bp.get("/api/workspaces/<int:workspace_id>/search")
def api_search(workspace_id: int):
    ws = load_workspace()
    db = get_db()
    query = request.args.get("q", "")
    limit, offset = _pagination(default_limit=20)
    gen = _active_generation(db, ws.id)
    hits, total = search_index.search(
        db, gen, query, limit=limit, offset=offset
    )
    return jsonify(
        {
            "query": query,
            "index_generation": gen.generation,
            "index_rule_version": gen.rule_version_id,
            "total": total,
            "limit": limit,
            "offset": offset,
            "results": [
                {
                    "document_id": h.workspace_document_id,
                    "title": h.title,
                    "sha256": h.sha256,
                    "score": round(h.score, 9),
                }
                for h in hits
            ],
        }
    )


@api_bp.get("/api/workspaces/<int:workspace_id>/documents/<int:doc_id>/keywords")
def api_keywords(workspace_id: int, doc_id: int):
    ws = load_workspace()
    db = get_db()
    docs_service.get_workspace_document(db, ws.id, doc_id)  # 404 if absent
    gen = _active_generation(db, ws.id)
    top_k = max(1, min(request.args.get("top_k", default=20, type=int), 100))
    kws = search_index.keywords(db, gen, doc_id, top_k=top_k)
    return jsonify(
        {
            "document_id": doc_id,
            "index_generation": gen.generation,
            "keywords": kws,
        }
    )
