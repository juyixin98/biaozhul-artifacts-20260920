"""文档上传（SHA-256 去重）、读取、删除（不泄漏）与实体/导出查询。"""
from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import select

from ..db import immediate_transaction, session_scope
from ..models import Document, Entity, Mention, Workspace, utcnow
from . import indexing, jobs as jobs_service


class WorkspaceError(Exception):
    pass


def _decode_utf8(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"只接受 UTF-8 文本：{exc}") from exc


def upload_document(workspace_id: int, name: str, raw: bytes) -> dict:
    """上传文档。

    - 内容按 SHA-256 全局去重（blobs.ref_count 引用计数，跨工作区共享字节）；
    - 工作区内同内容已有存活文档 → 返回 deduplicated；
    - 删除后重新上传视为新文档行（新 id、新作业）。
    """
    content = _decode_utf8(raw)
    name = (name or "untitled.txt")[:500]
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()

    with immediate_transaction() as conn:
        ws = conn.execute(
            "SELECT active_rule_pack_version, active_index_generation_id FROM workspaces WHERE id=?",
            (workspace_id,),
        ).fetchone()
        if ws is None:
            raise WorkspaceError("工作区不存在")
        rule_version = ws["active_rule_pack_version"]

        existing = conn.execute(
            "SELECT id FROM documents WHERE workspace_id=? AND doc_sha256=? AND deleted_at IS NULL",
            (workspace_id, sha),
        ).fetchone()

        # 内容字节全局复用
        blob = conn.execute("SELECT ref_count FROM blobs WHERE sha256=?", (sha,)).fetchone()
        if blob is None:
            conn.execute(
                "INSERT INTO blobs(sha256, content, byte_length, ref_count, created_at) "
                "VALUES (?,?,?,1,?)",
                (sha, content, len(raw), utcnow()),
            )
        else:
            conn.execute(
                "UPDATE blobs SET ref_count=ref_count+1 WHERE sha256=?", (sha,)
            )

        if existing is not None:
            return {
                "doc_id": int(existing["id"]),
                "sha256": sha,
                "deduplicated": True,
                "job_id": None,
            }

        # 首个索引代引导：工作区还没有任何代时，创建一个空的 active 代。
        if ws["active_index_generation_id"] is None:
            gcur = conn.execute(
                "INSERT INTO index_generations(workspace_id, rule_pack_version, status, "
                "doc_count, created_at, activated_at) VALUES (?,?, 'active', 0, ?, ?)",
                (workspace_id, rule_version, utcnow(), utcnow()),
            )
            gen_id = int(gcur.lastrowid)
            conn.execute(
                "UPDATE workspaces SET active_index_generation_id=? WHERE id=?",
                (gen_id, workspace_id),
            )

        now = utcnow()
        cur = conn.execute(
            "INSERT INTO documents(workspace_id, name, doc_sha256, blob_sha256, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (workspace_id, name, sha, sha, now, now),
        )
        doc_id = int(cur.lastrowid)
        job_id = jobs_service.enqueue_extract(
            conn,
            workspace_id=workspace_id,
            document_id=doc_id,
            rule_pack_version=rule_version,
            doc_sha256=sha,
        )
        return {
            "doc_id": doc_id,
            "sha256": sha,
            "deduplicated": False,
            "job_id": job_id,
            "active_rule_pack_version": rule_version,
        }


def _check_doc(conn, workspace_id: int, doc_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM documents WHERE workspace_id=? AND id=?", (workspace_id, doc_id)
    ).fetchone()
    if row is None:
        raise FileNotFoundError(f"文档 {doc_id} 不存在或不属于该工作区")
    if row["deleted_at"] is not None:
        raise FileNotFoundError(f"文档 {doc_id} 已删除")
    return dict(row)


def list_documents(workspace_id: int) -> list[dict]:
    with session_scope() as sess:
        rows = sess.execute(
            select(Document)
            .where(Document.workspace_id == workspace_id, Document.deleted_at.is_(None))
            .order_by(Document.id)
        ).scalars()
        return [_doc_dict(r) for r in rows]


def get_document(workspace_id: int, doc_id: int) -> dict:
    with immediate_transaction() as conn:
        row = _check_doc(conn, workspace_id, doc_id)
        return _row_dict(row)


def get_content(workspace_id: int, doc_id: int) -> dict:
    with immediate_transaction() as conn:
        row = _check_doc(conn, workspace_id, doc_id)
        blob = conn.execute(
            "SELECT content FROM blobs WHERE sha256=?", (row["blob_sha256"],)
        ).fetchone()
        return {
            "doc_id": doc_id,
            "name": row["name"],
            "sha256": row["doc_sha256"],
            "content": blob["content"] if blob else None,
        }


def delete_document(workspace_id: int, doc_id: int) -> dict:
    """单事务硬删除全部派生数据并在引用归零后清除内容字节。

    与正在跑的作业竞争时：作业事务在同一 IMMEDIATE 写锁后执行，复查文档发现
    deleted_at 非空即整体丢弃结果（见 extractor.get_fresh_blob），删除先提交则
    作业的更新因行被标记/作业取消而不落库。
    """
    with immediate_transaction() as conn:
        row = conn.execute(
            "SELECT id, blob_sha256, deleted_at FROM documents "
            "WHERE workspace_id=? AND id=?",
            (workspace_id, doc_id),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"文档 {doc_id} 不存在或不属于该工作区")
        if row["deleted_at"] is not None:
            return {"doc_id": doc_id, "deleted": True, "idempotent": True}

        blob_sha = row["blob_sha256"]

        # 1) 取消该文档尚未完成的作业（已完成的作业历史保留作审计）
        conn.execute(
            "UPDATE jobs SET status='failed', leased_by=NULL, leased_until=NULL, "
            "finished_at=?, updated_at=?, error='document deleted', error_stage='delete' "
            "WHERE document_id=? AND status IN ('pending','running')",
            (utcnow(), utcnow(), doc_id),
        )

        # 2) 倒排/统计（所有代）；同时收敛 active 代 df/权重
        indexing.purge_document_from_indexes(conn, workspace_id, doc_id)

        # 3) 实体与提及
        conn.execute(
            "DELETE FROM mentions WHERE entity_id IN "
            "(SELECT id FROM entities WHERE document_id=?)",
            (doc_id,),
        )
        conn.execute("DELETE FROM entities WHERE document_id=?", (doc_id,))

        # 4) 解除 blob 引用并软标记文档行（保留行做存在性审计，名字抹除）
        conn.execute(
            "UPDATE documents SET deleted_at=?, updated_at=?, name='<tombstone>', "
            "blob_sha256=NULL WHERE id=?",
            (utcnow(), utcnow(), doc_id),
        )
        if blob_sha is not None:
            new_ref = conn.execute(
                "UPDATE blobs SET ref_count=ref_count-1 WHERE sha256=? "
                "RETURNING ref_count",
                (blob_sha,),
            ).fetchone()
            if new_ref is not None and int(new_ref["ref_count"]) <= 0:
                # 最后一份引用消失 → 物理清除字节（其他工作区无权再借内容）
                conn.execute("DELETE FROM blobs WHERE sha256=?", (blob_sha,))

        return {"doc_id": doc_id, "deleted": True, "idempotent": False}


def list_entities(workspace_id: int, doc_id: int, rule_version: str | None = None) -> dict:
    with immediate_transaction() as conn:
        _check_doc(conn, workspace_id, doc_id)
        ver = rule_version or conn.execute(
            "SELECT active_rule_pack_version FROM workspaces WHERE id=?", (workspace_id,)
        ).fetchone()["active_rule_pack_version"]
        ent_rows = conn.execute(
            "SELECT id, entity_type, canonical, first_seen_rule_id, rule_pack_version "
            "FROM entities WHERE document_id=? AND rule_pack_version=? "
            "ORDER BY entity_type, canonical, id",
            (doc_id, ver),
        ).fetchall()
        entities = []
        for e in ent_rows:
            mentions = [
                {
                    "alias": m["alias"],
                    "matched_text": m["matched_text"],
                    "char_start": m["char_start"],
                    "char_end": m["char_end"],
                    "rule_id": m["rule_id"],
                }
                for m in conn.execute(
                    "SELECT alias, matched_text, char_start, char_end, rule_id "
                    "FROM mentions WHERE entity_id=? ORDER BY char_start, id",
                    (e["id"],),
                ).fetchall()
            ]
            entities.append(
                {
                    "entity_type": e["entity_type"],
                    "canonical": e["canonical"],
                    "first_seen_rule_id": e["first_seen_rule_id"],
                    "mentions": mentions,
                }
            )
        return {
            "doc_id": doc_id,
            "rule_pack_version": ver,
            "entity_count": len(entities),
            "entities": entities,
        }


def export_workspace(workspace_id: int) -> list[dict]:
    """NDJSON 导出：逐文档输出当前 active 规则版本的实体证据 + 关键词。"""
    with session_scope() as sess:
        ws = sess.get(Workspace, workspace_id)
        if ws is None:
            raise FileNotFoundError("工作区不存在")
        docs = sess.execute(
            select(Document)
            .where(Document.workspace_id == workspace_id, Document.deleted_at.is_(None))
            .order_by(Document.id)
        ).scalars().all()
        records: list[dict] = []
        for d in docs:
            ents = sess.execute(
                select(Entity)
                .where(
                    Entity.document_id == d.id,
                    Entity.rule_pack_version == ws.active_rule_pack_version,
                )
                .order_by(Entity.entity_type, Entity.canonical)
            ).scalars().all()
            ent_out = []
            for e in ents:
                mentions = sess.execute(
                    select(Mention)
                    .where(Mention.entity_id == e.id)
                    .order_by(Mention.char_start)
                ).scalars().all()
                ent_out.append(
                    {
                        "entity_type": e.entity_type,
                        "canonical": e.canonical,
                        "first_seen_rule_id": e.first_seen_rule_id,
                        "mentions": [
                            {
                                "alias": mm.alias,
                                "matched_text": mm.matched_text,
                                "char_start": mm.char_start,
                                "char_end": mm.char_end,
                                "rule_id": mm.rule_id,
                            }
                            for mm in mentions
                        ],
                    }
                )
            records.append(
                {
                    "doc_id": d.id,
                    "name": d.name,
                    "sha256": d.doc_sha256,
                    "rule_pack_version": ws.active_rule_pack_version,
                    "index_generation_id": ws.active_index_generation_id,
                    "entities": ent_out,
                }
            )
        return records


def _doc_dict(r: Document) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "sha256": r.doc_sha256,
        "created_at": r.created_at.isoformat() + "Z",
        "updated_at": r.updated_at.isoformat() + "Z",
    }


def _row_dict(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "sha256": row["doc_sha256"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _iso(v) -> str:
    if isinstance(v, datetime):
        return v.isoformat() + "Z"
    return str(v) + "Z" if v else None
