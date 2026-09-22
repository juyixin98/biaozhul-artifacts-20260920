"""HTTP API：工作区/文档/作业/搜索 + 规则管理。"""
from __future__ import annotations

import json

from flask import Blueprint, Response, jsonify, request, stream_with_context

from ..config import config
from ..services import documents as docs_service
from ..services import indexing, rule_packs, workspaces as ws_service
from ..services import jobs as jobs_service
from ..services import rebuild as rebuild_service

api_bp = Blueprint("api", __name__, url_prefix="/api")
admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


# --------------------------------------------------------------------------- #
# 认证辅助
# --------------------------------------------------------------------------- #

def _workspace():
    key = request.headers.get("X-Workspace-Key", "")
    return ws_service.authenticate(key)


def _require_admin():
    key = request.headers.get("X-Admin-Key", "")
    if key != config.admin_key:
        raise ws_service.AuthError("管理接口需要 X-Admin-Key")


def _json_body() -> dict:
    if not request.data:
        return {}
    try:
        body = json.loads(request.data.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"请求体不是合法 JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return body


# --------------------------------------------------------------------------- #
# 工作区
# --------------------------------------------------------------------------- #

@api_bp.post("/workspaces")
def create_workspace():
    body = _json_body()
    return jsonify(ws_service.create_workspace(str(body.get("name", "")))), 201


# --------------------------------------------------------------------------- #
# 文档
# --------------------------------------------------------------------------- #

@api_bp.post("/documents")
def upload_document():
    ws = _workspace()
    f = request.files.get("file")
    if f is None:
        # 也允许原始 body 上传（Content-Type: text/plain）
        if request.data:
            raw = request.data
            name = request.headers.get("X-Document-Name", "untitled.txt")
        else:
            raise ValueError("需要 multipart 字段 file 或 text/plain 请求体")
    else:
        raw = f.read()
        name = f.filename or "untitled.txt"
    result = docs_service.upload_document(ws.id, name, raw)
    return jsonify(result), 201


@api_bp.get("/documents")
def list_documents():
    ws = _workspace()
    return jsonify({"documents": docs_service.list_documents(ws.id)})


@api_bp.get("/documents/<int:doc_id>")
def get_document(doc_id: int):
    ws = _workspace()
    return jsonify(docs_service.get_document(ws.id, doc_id))


@api_bp.get("/documents/<int:doc_id>/content")
def get_content(doc_id: int):
    ws = _workspace()
    return jsonify(docs_service.get_content(ws.id, doc_id))


@api_bp.delete("/documents/<int:doc_id>")
def delete_document(doc_id: int):
    ws = _workspace()
    return jsonify(docs_service.delete_document(ws.id, doc_id))


@api_bp.get("/documents/<int:doc_id>/entities")
def get_entities(doc_id: int):
    ws = _workspace()
    version = request.args.get("rule_pack_version")
    return jsonify(docs_service.list_entities(ws.id, doc_id, version))


@api_bp.get("/documents/<int:doc_id>/keywords")
def get_keywords(doc_id: int):
    ws = _workspace()
    top_k = min(int(request.args.get("top_k", "10")), 100)
    return jsonify(indexing.document_keywords(ws.id, doc_id, top_k))


# --------------------------------------------------------------------------- #
# 作业
# --------------------------------------------------------------------------- #

@api_bp.get("/jobs")
def list_jobs():
    ws = _workspace()
    status = request.args.get("status")
    limit = min(int(request.args.get("limit", "100")), 500)
    return jsonify({"jobs": jobs_service.list_jobs(ws.id, status, limit)})


@api_bp.get("/jobs/<int:job_id>")
def get_job(job_id: int):
    ws = _workspace()
    return jsonify(jobs_service.get_job(job_id, ws.id))


@api_bp.post("/jobs/<int:job_id>/retry")
def retry_job(job_id: int):
    ws = _workspace()
    jobs_service.get_job(job_id, ws.id)  # 跨工作区 → 404
    jobs_service.retry_job(job_id)
    return jsonify({"retried": True, "job": jobs_service.get_job(job_id, ws.id)})


# --------------------------------------------------------------------------- #
# 搜索 / 关键词 / 导出
# --------------------------------------------------------------------------- #

@api_bp.get("/search")
def search():
    ws = _workspace()
    q = request.args.get("q", "")
    if not q.strip():
        raise ValueError("参数 q 不能为空")
    top_k = min(int(request.args.get("top_k", "10")), 100)
    return jsonify(indexing.search(ws.id, q, top_k))


@api_bp.get("/export")
def export():
    ws = _workspace()
    fmt = request.args.get("format", "ndjson").lower()
    records = docs_service.export_workspace(ws.id)
    if fmt == "json":
        return jsonify({"workspace_id": ws.id, "documents": records})

    def generate():
        for rec in records:
            yield json.dumps(rec, ensure_ascii=False) + "\n"

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=export.ndjson"},
    )


# --------------------------------------------------------------------------- #
# 管理接口
# --------------------------------------------------------------------------- #

@admin_bp.post("/rule-packs")
def publish_rule_pack():
    _require_admin()
    f = request.files.get("file")
    if f is not None:
        content = f.read().decode("utf-8")
    elif request.data:
        content = request.data.decode("utf-8")
    else:
        raise ValueError("需要 multipart 字段 file 或 JSON body")
    compiled, created = rule_packs.publish_pack(content)
    return jsonify(
        {
            "version": compiled.version,
            "published": created,
            "rule_count": len(compiled.rules),
        }
    ), (201 if created else 200)


@admin_bp.get("/rule-packs")
def list_rule_packs():
    _require_admin()
    return jsonify({"rule_packs": rule_packs.list_packs()})


@admin_bp.post("/workspaces/<int:workspace_id>/activate-rule")
def activate_rule(workspace_id: int):
    _require_admin()
    body = _json_body()
    version = body.get("version")
    if not version:
        raise ValueError("body 需要 version")
    # 确认工作区存在（管理员接口不接受工作区密钥）
    ws_service.get_workspace(workspace_id)
    return jsonify(rebuild_service.activate_rule(workspace_id, version)), 202


@admin_bp.post("/workspaces/<int:workspace_id>/rollback-rule")
def rollback_rule(workspace_id: int):
    _require_admin()
    body = _json_body()
    version = body.get("version")
    if not version:
        raise ValueError("body 需要 version")
    ws_service.get_workspace(workspace_id)
    return jsonify(rebuild_service.rollback_rule(workspace_id, version)), 202


@admin_bp.get("/workspaces/<int:workspace_id>/rule")
def get_rule(workspace_id: int):
    _require_admin()
    ws_service.get_workspace(workspace_id)
    return jsonify(rebuild_service.current_rule(workspace_id))
