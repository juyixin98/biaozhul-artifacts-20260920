"""作业队列表 + 工作器租约。

认领路径使用 BEGIN IMMEDIATE 单连接事务，读-判-写原子，最多 ``KEX_MAX_WORKERS``
个租约同时存在（跨进程/容器一致）。作业状态、错误阶段与检查点全部落库。
"""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select

from ..config import config
from ..db import immediate_transaction, session_scope
from ..models import Job, utcnow

# priority 越小越先执行：普通抽取优先于重建。
PRIORITY_EXTRACT = 10
PRIORITY_REBUILD = 50


class JobNotFound(Exception):
    pass


# --------------------------------------------------------------------------- #
# 入队（均在 IMMEDIATE 事务内，串行安全）
# --------------------------------------------------------------------------- #

def _now():
    return utcnow()


def enqueue_extract(
    conn,
    *,
    workspace_id: int,
    document_id: int,
    rule_pack_version: str,
    doc_sha256: str,
) -> int:
    """在已有 IMMEDIATE 连接上入队抽取作业；同文档/同版本已有未完成作业则复用。"""
    row = conn.execute(
        "SELECT id FROM jobs "
        "WHERE workspace_id=? AND document_id=? AND kind='extract' "
        "AND rule_pack_version=? AND status IN ('pending','running')",
        (workspace_id, document_id, rule_pack_version),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO jobs(workspace_id, document_id, kind, status, rule_pack_version, "
        "doc_sha256, priority, max_attempts, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            workspace_id, document_id, "extract", "pending", rule_pack_version,
            doc_sha256, PRIORITY_EXTRACT, config.max_attempts, _now(), _now(),
        ),
    )
    return int(cur.lastrowid)


def enqueue_extract_standalone(
    *, workspace_id: int, document_id: int, rule_pack_version: str, doc_sha256: str
) -> int:
    with immediate_transaction() as conn:
        return enqueue_extract(
            conn,
            workspace_id=workspace_id,
            document_id=document_id,
            rule_pack_version=rule_pack_version,
            doc_sha256=doc_sha256,
        )


def has_running_rebuild(conn, workspace_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE workspace_id=? AND kind='rebuild' "
        "AND status IN ('pending','running') LIMIT 1",
        (workspace_id,),
    ).fetchone()
    return row is not None


def enqueue_rebuild(
    *, workspace_id: int, rule_pack_version: str, generation_id: int
) -> int:
    payload = json.dumps({"generation_id": generation_id})
    with immediate_transaction() as conn:
        if has_running_rebuild(conn, workspace_id):
            raise RebuildInProgress(workspace_id)
        cur = conn.execute(
            "INSERT INTO jobs(workspace_id, document_id, kind, status, rule_pack_version, "
            "doc_sha256, payload_json, priority, max_attempts, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                workspace_id, None, "rebuild", "pending", rule_pack_version, None,
                payload, PRIORITY_REBUILD, config.max_attempts, _now(), _now(),
            ),
        )
        return int(cur.lastrowid)


class RebuildInProgress(Exception):
    def __init__(self, workspace_id: int):
        super().__init__(f"工作区 {workspace_id} 已有重建任务在执行")
        self.workspace_id = workspace_id


# --------------------------------------------------------------------------- #
# 租约与认领
# --------------------------------------------------------------------------- #

def heartbeat(worker_id: str) -> None:
    with immediate_transaction() as conn:
        conn.execute(
            "INSERT INTO worker_registry(worker_id, heartbeat_at, started_at) "
            "VALUES(?,?,?) ON CONFLICT(worker_id) DO UPDATE SET heartbeat_at=excluded.heartbeat_at",
            (worker_id, _now(), _now()),
        )
        # 顺带把本工作器名下过期的租约视作自己还在持有（心跳保持期间不过期）。
        conn.execute(
            "UPDATE worker_registry SET heartbeat_at=? WHERE worker_id=?",
            (_now(), worker_id),
        )


def _active_lease_count(conn) -> int:
    cutoff = _now() - timedelta(seconds=config.lease_seconds)
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM jobs WHERE status='running' AND leased_until > ?",
        (cutoff,),
    ).fetchone()
    return int(row["c"])


def _reap_expired(conn) -> int:
    """把租约过期的 running 作业退回 pending（崩溃/被杀进程的恢复路径）。"""
    cutoff = _now() - timedelta(seconds=config.lease_seconds)
    cur = conn.execute(
        "UPDATE jobs SET status='pending', leased_by=NULL, leased_until=NULL, updated_at=? "
        "WHERE status='running' AND leased_until <= ?",
        (_now(), cutoff),
    )
    return cur.rowcount


def claim_job(worker_id: str) -> dict | None:
    """原子认领一个作业。超过租约上限则不领。返回作业字典或 None。"""
    with immediate_transaction() as conn:
        _reap_expired(conn)
        if _active_lease_count(conn) >= config.max_workers:
            return None
        row = conn.execute(
            "SELECT * FROM jobs WHERE status='pending' "
            "ORDER BY priority ASC, id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        lease_until = _now() + timedelta(seconds=config.lease_seconds)
        conn.execute(
            "UPDATE jobs SET status='running', leased_by=?, leased_until=?, "
            "attempts=attempts+1, updated_at=? WHERE id=?",
            (worker_id, lease_until, _now(), row["id"]),
        )
        # 返回更新后的行（attempts/状态已变），而不是更新前快照
        job_row = conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        job = dict(job_row)
        job["status"] = "running"
        job["leased_by"] = worker_id
        job["leased_until"] = lease_until
        return job


def renew_lease(job_id: int, worker_id: str) -> bool:
    with immediate_transaction() as conn:
        lease_until = _now() + timedelta(seconds=config.lease_seconds)
        cur = conn.execute(
            "UPDATE jobs SET leased_until=?, updated_at=? "
            "WHERE id=? AND status='running' AND leased_by=?",
            (lease_until, _now(), job_id, worker_id),
        )
        return cur.rowcount == 1


def complete_job(job_id: int, checkpoint: dict | None = None) -> None:
    with immediate_transaction() as conn:
        conn.execute(
            "UPDATE jobs SET status='succeeded', error=NULL, error_stage=NULL, "
            "error_doc_id=NULL, leased_by=NULL, leased_until=NULL, finished_at=?, "
            "updated_at=?, checkpoint_json=? WHERE id=?",
            (_now(), _now(), json.dumps(checkpoint, ensure_ascii=False) if checkpoint else None, job_id),
        )


def fail_job(job_id: int, *, stage: str, doc_id: int | None, message: str) -> bool:
    """记录失败。超过最大尝试次数置 failed，否则退回 pending 等待重试。"""
    with immediate_transaction() as conn:
        row = conn.execute(
            "SELECT attempts, max_attempts FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            return False
        if int(row["attempts"]) >= int(row["max_attempts"]):
            conn.execute(
                "UPDATE jobs SET status='failed', error=?, error_stage=?, error_doc_id=?, "
                "leased_by=NULL, leased_until=NULL, finished_at=?, updated_at=? WHERE id=?",
                (message[:4000], stage, doc_id, _now(), _now(), job_id),
            )
            return False  # 不会自动重试
        conn.execute(
            "UPDATE jobs SET status='pending', error=?, error_stage=?, error_doc_id=?, "
            "leased_by=NULL, leased_until=NULL, updated_at=? WHERE id=?",
            (message[:4000], stage, doc_id, _now(), job_id),
        )
        return True


def save_checkpoint(job_id: int, checkpoint: dict) -> None:
    with immediate_transaction() as conn:
        save_checkpoint_conn(conn, job_id, checkpoint)


def save_checkpoint_conn(conn, job_id: int, checkpoint: dict) -> None:
    """在已有 IMMEDIATE 连接上写检查点（嵌套调用时必须用这个，避免写锁自锁）。"""
    conn.execute(
        "UPDATE jobs SET checkpoint_json=?, updated_at=? WHERE id=?",
        (json.dumps(checkpoint, ensure_ascii=False), _now(), job_id),
    )


def retry_job(job_id: int) -> dict:
    with immediate_transaction() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFound(str(job_id))
        if row["status"] not in ("failed", "pending"):
            raise ValueError(f"作业状态为 {row['status']}，仅 failed/pending 可重试")
        conn.execute(
            "UPDATE jobs SET status='pending', attempts=0, error=NULL, error_stage=NULL, "
            "error_doc_id=NULL, leased_by=NULL, leased_until=NULL, updated_at=? WHERE id=?",
            (_now(), job_id),
        )
        return dict(row)


def get_job(job_id: int, workspace_id: int | None = None) -> dict:
    with session_scope() as sess:
        q = select(Job).where(Job.id == job_id)
        if workspace_id is not None:
            q = q.where(Job.workspace_id == workspace_id)
        row = sess.execute(q).scalar_one_or_none()
        if row is None:
            raise JobNotFound(str(job_id))
        return _job_dict(row)


def list_jobs(workspace_id: int, status: str | None = None, limit: int = 100) -> list[dict]:
    with session_scope() as sess:
        q = select(Job).where(Job.workspace_id == workspace_id)
        if status:
            q = q.where(Job.status == status)
        q = q.order_by(Job.id.desc()).limit(limit)
        return [_job_dict(r) for r in sess.execute(q).scalars()]


def _job_dict(row: Job) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "document_id": row.document_id,
        "kind": row.kind,
        "status": row.status,
        "rule_pack_version": row.rule_pack_version,
        "doc_sha256": row.doc_sha256,
        "payload": json.loads(row.payload_json) if row.payload_json else None,
        "priority": row.priority,
        "attempts": row.attempts,
        "max_attempts": row.max_attempts,
        "error": row.error,
        "error_stage": row.error_stage,
        "error_doc_id": row.error_doc_id,
        "checkpoint": json.loads(row.checkpoint_json) if row.checkpoint_json else None,
        "leased_by": row.leased_by,
        "created_at": row.created_at.isoformat() + "Z",
        "finished_at": row.finished_at.isoformat() + "Z" if row.finished_at else None,
    }
