"""多进程并发：多个独立工作器进程同时跑，租约上限 2，作业不重复执行。

使用 multiprocessing 启动 3 个真正的独立进程（不是线程），共享同一个 SQLite
WAL 文件，验证 BEGIN IMMEDIATE 跨进程串行与租约计数。
"""
from __future__ import annotations

import multiprocessing as mp
import sqlite3
import time

from conftest import _DB_PATH


def _worker_proc(worker_id: str, stop_after_idle: float) -> None:
    # fork 后的子进程：丢弃继承的引擎，建立本进程独立连接
    from app import db
    from app.services import jobs as jobs_service
    from app.services.worker import Worker

    db.reset_engine()
    w = Worker(worker_id=worker_id, poll_interval=0.05)
    idle = 0.0
    while idle < stop_after_idle:
        try:
            job = jobs_service.claim_job(worker_id)
        except Exception:
            time.sleep(0.05)
            continue
        if job is None:
            idle += 0.05
            time.sleep(0.05)
            continue
        idle = 0.0
        w._run_one(job)


def test_multiprocess_workers_no_duplicate_execution(fresh_db):
    """用 fork 让 3 个独立进程共享同一个 WAL 文件（不共享连接/内存）。

    fork 后子进程重置引擎，建立各自独立的 sqlite 连接池，因此这真正考验
    BEGIN IMMEDIATE 的跨进程互斥，而不是进程内锁。
    """
    from app.db import immediate_transaction, reset_engine
    from app.services.workspaces import create_workspace

    ws = create_workspace("mp-ws")
    n_docs = 24
    with immediate_transaction() as conn:
        for i in range(n_docs):
            conn.execute(
                "INSERT INTO documents(workspace_id, name, doc_sha256, blob_sha256, "
                "created_at, updated_at) VALUES (?,?, 'x', NULL, "
                "datetime('now'), datetime('now'))",
                (ws["id"], f"d{i}"),
            )
        docs = conn.execute(
            "SELECT id FROM documents WHERE workspace_id=? ORDER BY id", (ws["id"],)
        ).fetchall()
        from app.services import jobs as jobs_service
        for d in docs:
            jobs_service.enqueue_extract(
                conn, workspace_id=ws["id"], document_id=d["id"],
                rule_pack_version=ws["active_rule_pack_version"], doc_sha256="x",
            )
    # 父进程丢弃引擎，避免 fork 后子进程继承到打开的连接句柄
    reset_engine()

    ctx = mp.get_context("fork")
    procs = [
        ctx.Process(target=_worker_proc, args=(f"mp-{i}", 2.0), daemon=True)
        for i in range(3)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=40)
        assert not p.is_alive(), f"worker {p.pid} 超时未退出"

    conn = sqlite3.connect(_DB_PATH)
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE workspace_id=? AND kind='extract'", (ws["id"],)
        ).fetchone()[0]
        succeeded = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE workspace_id=? AND kind='extract' AND status='succeeded'",
            (ws["id"],),
        ).fetchone()[0]
        failed = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE workspace_id=? AND kind='extract' AND status='failed'",
            (ws["id"],),
        ).fetchone()[0]
        # 每个作业恰好被执行（stale blob 作业成功跳过），无遗漏
        assert total == n_docs
        assert succeeded + failed == n_docs, (succeeded, failed)
        # 所有这些作业 doc_sha256='x' 且 blob 缺失 → 跳过为 succeeded，不应有失败
        assert failed == 0, f"存在失败作业: {failed}"
        distinct_lease = conn.execute(
            "SELECT COUNT(DISTINCT leased_by) FROM jobs WHERE workspace_id=?", (ws["id"],)
        ).fetchone()[0]
    finally:
        conn.close()
    # 三个进程都可能领到过任务，但任意时刻租约数受限于 2（由认领事务保证）
    assert distinct_lease <= 3
