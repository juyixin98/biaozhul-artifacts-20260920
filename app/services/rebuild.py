"""规则版本编排：激活、回滚、重建调度。

不变量：
- 规则包内容不可变；激活/回滚都只写新的 activation 行，不改历史；
- 历史抽取证据（entities/mentions，带 rule_pack_version）永不被回滚改写；
- 重建期新文档不漏：切换 active 前在写锁事务内做最终覆盖校验，缺漏即不切。
"""
from __future__ import annotations

import json

from sqlalchemy import select

from ..db import immediate_transaction, session_scope
from ..models import IndexGeneration, RuleActivation, Workspace, utcnow
from . import indexing, jobs as jobs_service
from .rule_packs import load_pack


class ActivationError(Exception):
    pass


def activate_rule(workspace_id: int, version: str) -> dict:
    """激活新规则版本：指针立即切换，新文档走新规则；老文档全量重抽+重建索引。"""
    load_pack(version)  # 不存在则 KeyError
    with immediate_transaction() as conn:
        ws = conn.execute(
            "SELECT active_rule_pack_version FROM workspaces WHERE id=?", (workspace_id,)
        ).fetchone()
        if ws is None:
            raise ActivationError("工作区不存在")
        previous = ws["active_rule_pack_version"]
        if previous == version:
            return {"activated": False, "reason": "already_active", "version": version}
        if jobs_service.has_running_rebuild(conn, workspace_id):
            raise ActivationError("该工作区已有重建任务在执行，请等待其结束")
        # 已有 building 代说明编排状态不一致（上一次重建失败残留）
        building = conn.execute(
            "SELECT id FROM index_generations WHERE workspace_id=? AND status='building'",
            (workspace_id,),
        ).fetchone()
        if building is not None:
            raise ActivationError(f"存在未完成的索引代 {building['id']}，请先重试/放弃该重建")

        # 1) 切换规则指针（之后上传的新文档立即绑定新版本）
        conn.execute(
            "UPDATE workspaces SET active_rule_pack_version=? WHERE id=?",
            (version, workspace_id),
        )
        conn.execute(
            "INSERT INTO rule_activations(workspace_id, rule_pack_version, action, "
            "previous_version, activated_at) VALUES (?,?,?,?,?)",
            (workspace_id, version, "activate", previous, utcnow()),
        )

        # 2) 为所有存活文档创建新规则的抽取作业（旧证据保留）
        docs = conn.execute(
            "SELECT id, doc_sha256 FROM documents "
            "WHERE workspace_id=? AND deleted_at IS NULL ORDER BY id",
            (workspace_id,),
        ).fetchall()
        extract_job_ids = []
        for d in docs:
            jid = jobs_service.enqueue_extract(
                conn,
                workspace_id=workspace_id,
                document_id=int(d["id"]),
                rule_pack_version=version,
                doc_sha256=d["doc_sha256"],
            )
            extract_job_ids.append(jid)

        # 3) 建新一代索引并入队重建
        cur = conn.execute(
            "INSERT INTO index_generations(workspace_id, rule_pack_version, status, created_at) "
            "VALUES (?,?, 'building', ?)",
            (workspace_id, version, utcnow()),
        )
        generation_id = int(cur.lastrowid)
        payload = json.dumps({"generation_id": generation_id})
        curj = conn.execute(
            "INSERT INTO jobs(workspace_id, document_id, kind, status, rule_pack_version, "
            "doc_sha256, payload_json, priority, max_attempts, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                workspace_id, None, "rebuild", "pending", version, None,
                payload, jobs_service.PRIORITY_REBUILD, 5, utcnow(), utcnow(),
            ),
        )
        rebuild_job_id = int(curj.lastrowid)

    return {
        "activated": True,
        "version": version,
        "previous_version": previous,
        "generation_id": generation_id,
        "rebuild_job_id": rebuild_job_id,
        "extract_job_count": len(extract_job_ids),
    }


def rollback_rule(workspace_id: int, version: str) -> dict:
    """回滚：优先原子切回「覆盖完整」的旧代；否则为旧规则重新构建一代。

    无论哪条路径，历史 entities/mentions 都不删不改。
    """
    load_pack(version)
    with immediate_transaction() as conn:
        ws = conn.execute(
            "SELECT active_rule_pack_version, active_index_generation_id FROM workspaces WHERE id=?",
            (workspace_id,),
        ).fetchone()
        if ws is None:
            raise ActivationError("工作区不存在")
        previous = ws["active_rule_pack_version"]
        if previous == version:
            return {"rolled_back": False, "reason": "already_active", "version": version}
        if jobs_service.has_running_rebuild(conn, workspace_id):
            raise ActivationError("该工作区已有重建任务在执行，请等待其结束")

        # 路径 A：已有该版本、覆盖当前全部存活文档的代（active/superseded 都可能）
        candidate = conn.execute(
            "SELECT id, status FROM index_generations "
            "WHERE workspace_id=? AND rule_pack_version=? AND status IN ('active','superseded') "
            "ORDER BY id DESC",
            (workspace_id, version),
        ).fetchall()
        target_gen = None
        for g in candidate:
            missing = indexing.generation_missing_docs(conn, int(g["id"]), workspace_id)
            if not missing:
                target_gen = int(g["id"])
                break

        if target_gen is not None:
            # 指针 + 代次原子切换；历史证据不动
            cur_active = ws["active_index_generation_id"]
            conn.execute(
                "UPDATE index_generations SET status='superseded' "
                "WHERE workspace_id=? AND status='active' AND id<>?",
                (workspace_id, target_gen),
            )
            conn.execute(
                "UPDATE index_generations SET status='active', activated_at=? WHERE id=?",
                (utcnow(), target_gen),
            )
            conn.execute(
                "UPDATE workspaces SET active_rule_pack_version=?, "
                "active_index_generation_id=? WHERE id=?",
                (version, target_gen, workspace_id),
            )
            conn.execute(
                "INSERT INTO rule_activations(workspace_id, rule_pack_version, action, "
                "previous_version, activated_at) VALUES (?,?,?,?,?)",
                (workspace_id, version, "rollback", previous, utcnow()),
            )
            if cur_active is not None and int(cur_active) != target_gen:
                indexing._delete_generation_data(conn, int(cur_active))
                conn.execute(
                    "DELETE FROM index_generations WHERE id=? AND status='superseded'",
                    (int(cur_active),),
                )
            return {
                "rolled_back": True,
                "mode": "switch",
                "version": version,
                "generation_id": target_gen,
                "rebuild_job_id": None,
                "extract_job_count": 0,
            }

        # 路径 B：旧规则没有完整代 → 切换规则指针并重建（与激活同构）
        building = conn.execute(
            "SELECT id FROM index_generations WHERE workspace_id=? AND status='building'",
            (workspace_id,),
        ).fetchone()
        if building is not None:
            raise ActivationError(f"存在未完成的索引代 {building['id']}，请先处理")

        conn.execute(
            "UPDATE workspaces SET active_rule_pack_version=? WHERE id=?",
            (version, workspace_id),
        )
        conn.execute(
            "INSERT INTO rule_activations(workspace_id, rule_pack_version, action, "
            "previous_version, activated_at) VALUES (?,?,?,?,?)",
            (workspace_id, version, "rollback", previous, utcnow()),
        )
        docs = conn.execute(
            "SELECT id, doc_sha256 FROM documents "
            "WHERE workspace_id=? AND deleted_at IS NULL ORDER BY id",
            (workspace_id,),
        ).fetchall()
        extract_job_ids = []
        for d in docs:
            jid = jobs_service.enqueue_extract(
                conn,
                workspace_id=workspace_id,
                document_id=int(d["id"]),
                rule_pack_version=version,
                doc_sha256=d["doc_sha256"],
            )
            extract_job_ids.append(jid)
        cur = conn.execute(
            "INSERT INTO index_generations(workspace_id, rule_pack_version, status, created_at) "
            "VALUES (?,?, 'building', ?)",
            (workspace_id, version, utcnow()),
        )
        generation_id = int(cur.lastrowid)
        curj = conn.execute(
            "INSERT INTO jobs(workspace_id, document_id, kind, status, rule_pack_version, "
            "doc_sha256, payload_json, priority, max_attempts, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                workspace_id, None, "rebuild", "pending", version, None,
                json.dumps({"generation_id": generation_id}),
                jobs_service.PRIORITY_REBUILD, 5, utcnow(), utcnow(),
            ),
        )
        return {
            "rolled_back": True,
            "mode": "rebuild",
            "version": version,
            "generation_id": generation_id,
            "rebuild_job_id": int(curj.lastrowid),
            "extract_job_count": len(extract_job_ids),
        }


def activation_history(workspace_id: int) -> list[dict]:
    with session_scope() as sess:
        rows = sess.execute(
            select(RuleActivation)
            .where(RuleActivation.workspace_id == workspace_id)
            .order_by(RuleActivation.id)
        ).scalars()
        return [
            {
                "id": r.id,
                "rule_pack_version": r.rule_pack_version,
                "action": r.action,
                "previous_version": r.previous_version,
                "activated_at": r.activated_at.isoformat() + "Z",
            }
            for r in rows
        ]


def current_rule(workspace_id: int) -> dict:
    with session_scope() as sess:
        ws = sess.get(Workspace, workspace_id)
        if ws is None:
            raise ActivationError("工作区不存在")
        gens = sess.execute(
            select(IndexGeneration)
            .where(IndexGeneration.workspace_id == workspace_id)
            .order_by(IndexGeneration.id.desc())
            .limit(10)
        ).scalars()
        return {
            "active_rule_pack_version": ws.active_rule_pack_version,
            "active_index_generation_id": ws.active_index_generation_id,
            "recent_generations": [
                {
                    "id": g.id,
                    "rule_pack_version": g.rule_pack_version,
                    "status": g.status,
                    "doc_count": g.doc_count,
                    "error": g.error,
                    "created_at": g.created_at.isoformat() + "Z",
                }
                for g in gens
            ],
            "history": activation_history(workspace_id),
        }
