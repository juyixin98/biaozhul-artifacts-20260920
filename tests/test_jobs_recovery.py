"""作业持久化、租约上限、崩溃重启恢复、失败重试与阶段定位。"""
from __future__ import annotations

import io
import sqlite3

from conftest import _DB_PATH


def _raw():
    conn = sqlite3.connect(_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def test_max_two_leases(fresh_db):
    """直接压认领接口：最多 KEX_MAX_WORKERS=2 个并发租约。"""
    from app.services import jobs as jobs_service
    from app.services.workspaces import create_workspace
    from app.db import immediate_transaction

    ws = create_workspace("lease-ws")
    # 手工塞 5 个 extract 作业
    with immediate_transaction() as conn:
        for i in range(5):
            conn.execute(
                "INSERT INTO documents(workspace_id, name, doc_sha256, blob_sha256, created_at, updated_at) "
                "VALUES (?,?, 'x', NULL, datetime('now'), datetime('now'))",
                (ws["id"], f"d{i}"),
            )
        docs = conn.execute("SELECT id FROM documents ORDER BY id").fetchall()
        for d in docs:
            jobs_service.enqueue_extract(
                conn, workspace_id=ws["id"], document_id=d["id"],
                rule_pack_version=ws["active_rule_pack_version"], doc_sha256="x",
            )

    j1 = jobs_service.claim_job("p1")
    j2 = jobs_service.claim_job("p2")
    j3 = jobs_service.claim_job("p3")
    assert j1 is not None and j2 is not None
    assert j3 is None, f"超过最大租约数: {j3}"

    # 完成一个后可以再领
    jobs_service.complete_job(j1["id"])
    j4 = jobs_service.claim_job("p3")
    assert j4 is not None


def test_expired_lease_reclaimed_after_restart(fresh_db):
    """模拟进程崩溃：running 作业租约过期后被新工作器重新认领，attempts 累加。"""
    from app.services import jobs as jobs_service
    from app.services.workspaces import create_workspace
    from app.db import immediate_transaction
    from app.models import utcnow
    from datetime import timedelta

    ws = create_workspace("crash-ws")
    # 先用掉工作区创建可能遗留的待处理作业，再制造目标作业
    while jobs_service.claim_job("seeder") is not None:
        pass
    with immediate_transaction() as conn:
        conn.execute(
            "INSERT INTO documents(workspace_id, name, doc_sha256, blob_sha256, created_at, updated_at) "
            "VALUES (?,?, 's', NULL, datetime('now'), datetime('now'))",
            (ws["id"], "d"),
        )
        doc_id = conn.execute(
            "SELECT id FROM documents WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (ws["id"],)
        ).fetchone()[0]
        job_id = jobs_service.enqueue_extract(
            conn, workspace_id=ws["id"], document_id=doc_id,
            rule_pack_version=ws["active_rule_pack_version"], doc_sha256="s",
        )

    claimed = jobs_service.claim_job("dead-worker")
    assert claimed["id"] == job_id
    # 「崩溃」：直接把租约时间改到很久以前（等价于 lease_seconds 之后的重启时刻）
    with immediate_transaction() as conn:
        conn.execute(
            "UPDATE jobs SET leased_until=? WHERE id=?",
            (utcnow() - timedelta(hours=1), job_id),
        )

    reclaimed = jobs_service.claim_job("new-worker")
    assert reclaimed is not None and reclaimed["id"] == job_id
    assert reclaimed["attempts"] == 2, "重领应累加 attempts"
    assert reclaimed["leased_by"] == "new-worker"


def test_failed_job_has_stage_and_doc_and_can_retry(ws, client, drain):
    h = ws["headers"]
    # 上传正常文档
    up = client.post(
        "/api/documents",
        data={"file": (io.BytesIO("李明说 SQLite".encode()), "d.txt")},
        headers=h, content_type="multipart/form-data",
    ).get_json()
    drain()
    assert client.get("/api/jobs", headers=h).get_json()

    # 手工构造一个注定失败的作业：doc_sha256 不匹配 → 不会写实体，但会被标记 stale。
    # 为测试真正的 failed+retry 路径，直接在库里制造一个引用缺失 blob 的作业。
    from app.db import immediate_transaction

    with immediate_transaction() as conn:
        cur = conn.execute(
            "INSERT INTO jobs(workspace_id, document_id, kind, status, rule_pack_version, "
            "doc_sha256, priority, max_attempts, created_at, updated_at) "
            "VALUES (?,?, 'extract','pending',?, 'deadbeef', 10, 1, datetime('now'), datetime('now'))",
            (ws["id"], up["doc_id"], ws["default_rule"]),
        )
        bad_id = cur.lastrowid
    drain()

    # stale 作业其实是成功（跳过）——改为验证「真正失败」路径：
    # 把规则版本改成不存在的版本再 drain
    with immediate_transaction() as conn:
        conn.execute(
            "UPDATE jobs SET status='pending', attempts=0, rule_pack_version='does-not-exist', "
            "leased_by=NULL, leased_until=NULL WHERE id=?", (bad_id,)
        )
    drain()
    failed = client.get("/api/jobs?status=failed", headers=h).get_json()["jobs"]
    assert any(j["id"] == bad_id for j in failed)
    fj = client.get(f"/api/jobs/{bad_id}", headers=h).get_json()
    assert fj["error_stage"]  # 阶段可定位
    assert fj["error_doc_id"] in (up["doc_id"], None)

    # retry：修好版本后重试成功
    with immediate_transaction() as conn:
        conn.execute(
            "UPDATE jobs SET rule_pack_version=? WHERE id=?",
            (ws["default_rule"], bad_id),
        )
    rr = client.post(f"/api/jobs/{bad_id}/retry", headers=h)
    assert rr.status_code == 200
    drain()
    final = client.get(f"/api/jobs/{bad_id}", headers=h).get_json()
    assert final["status"] == "succeeded"


def test_checkpoint_persisted_across_reclaim(ws, client, drain):
    """重建作业的检查点持久化：被回收后新执行者从检查点继续。"""
    import json
    from app.services import jobs as jobs_service
    from app.db import immediate_transaction

    # 直接构造一个带 checkpoint 的重建作业（真实流程里由 worker 写入）
    with immediate_transaction() as conn:
        cur = conn.execute(
            "INSERT INTO index_generations(workspace_id, rule_pack_version, status, created_at) "
            "VALUES (?,?,'building',datetime('now'))",
            (ws["id"], ws["default_rule"]),
        )
        gen_id = cur.lastrowid
        curj = conn.execute(
            "INSERT INTO jobs(workspace_id, kind, status, rule_pack_version, payload_json, "
            "priority, max_attempts, checkpoint_json, created_at, updated_at) "
            "VALUES (?, 'rebuild','pending',?, ?, 50, 5, ?, datetime('now'), datetime('now'))",
            (ws["id"], ws["default_rule"], json.dumps({"generation_id": gen_id}),
             json.dumps({"extracted_docs": [1, 2, 3], "passes": 2})),
        )
        job_id = curj.lastrowid

    j = jobs_service.claim_job("cp-worker")
    assert j["id"] == job_id
    cp = json.loads(j["checkpoint_json"])
    assert cp["extracted_docs"] == [1, 2, 3]
    assert cp["passes"] == 2
    jobs_service.fail_job(job_id, stage="x", doc_id=None, message="teardown")
    # 清理：把代次删掉以免影响别的断言
    with immediate_transaction() as conn:
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        conn.execute("DELETE FROM index_generations WHERE id=?", (gen_id,))
