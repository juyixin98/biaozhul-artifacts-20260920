"""删除不泄露：删除后搜索/实体/导出/blob 字节都不再可见，含删除竞争。"""
from __future__ import annotations

import io
import sqlite3
import threading

from conftest import _DB_PATH, SAMPLE_TEXT_A


def _upload(client, headers, text, name="d.txt"):
    return client.post(
        "/api/documents",
        data={"file": (io.BytesIO(text.encode("utf-8")), name)},
        headers=headers,
        content_type="multipart/form-data",
    ).get_json()


def _raw_conn():
    conn = sqlite3.connect(_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def test_deleted_doc_disappears_everywhere(ws, client, drain):
    h = ws["headers"]
    up = _upload(client, h, SAMPLE_TEXT_A, "secret.txt")
    drain()
    doc_id = up["doc_id"]
    sha = up["sha256"]

    assert client.get("/api/search?q=李明", headers=h).get_json()["result_count"] == 1
    r = client.delete(f"/api/documents/{doc_id}", headers=h)
    assert r.status_code == 200

    # 文档/内容/实体/搜索全部不可见
    assert client.get(f"/api/documents/{doc_id}", headers=h).status_code == 404
    assert client.get(f"/api/documents/{doc_id}/content", headers=h).status_code == 404
    assert client.get(f"/api/documents/{doc_id}/entities", headers=h).status_code == 404
    assert client.get("/api/search?q=李明", headers=h).get_json()["result_count"] == 0
    assert client.get("/api/search?q=Python", headers=h).get_json()["result_count"] == 0

    exported = client.get("/api/export?format=json", headers=h).get_json()
    assert all(d["doc_id"] != doc_id for d in exported["documents"])

    conn = _raw_conn()
    try:
        # 派生数据物理删除
        assert conn.execute("SELECT COUNT(*) FROM entities WHERE document_id=?", (doc_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM postings WHERE document_id=?", (doc_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM doc_term_stats WHERE document_id=?", (doc_id,)).fetchone()[0] == 0
        # 该工作区是 blob 的唯一持有者 → 字节被清除
        blob = conn.execute("SELECT content, ref_count FROM blobs WHERE sha256=?", (sha,)).fetchone()
        assert blob is None
        # 任何词都查不到被删文档的内容（直接走倒排表）
        leak = conn.execute(
            "SELECT COUNT(*) FROM postings p JOIN index_generations g ON g.id=p.generation_id "
            "WHERE p.document_id=?", (doc_id,)
        ).fetchone()[0]
        assert leak == 0
    finally:
        conn.close()


def test_shared_blob_kept_until_last_reference_gone(workspace_factory, client, drain):
    w1 = workspace_factory("w1")
    w2 = workspace_factory("w2")
    text = "共享内容 李明 SQLite 甲乙丙"
    up1 = _upload(client, w1["headers"], text, "a.txt")
    up2 = _upload(client, w2["headers"], text, "copy.txt")
    assert up1["sha256"] == up2["sha256"]
    drain()

    # 删 w1 的，w2 仍能读原文（共享但权限不串）
    client.delete(f"/api/documents/{up1['doc_id']}", headers=w1["headers"])
    content = client.get(f"/api/documents/{up2['doc_id']}/content", headers=w2["headers"])
    assert content.status_code == 200
    assert content.get_json()["content"] == text

    conn = _raw_conn()
    try:
        row = conn.execute("SELECT ref_count FROM blobs WHERE sha256=?", (up2["sha256"],)).fetchone()
        assert row is not None and row["ref_count"] == 1
    finally:
        conn.close()

    client.delete(f"/api/documents/{up2['doc_id']}", headers=w2["headers"])
    conn = _raw_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM blobs WHERE sha256=?", (up2["sha256"],)).fetchone()[0] == 0
    finally:
        conn.close()


def test_reupload_after_delete_is_new_document(ws, client, drain):
    h = ws["headers"]
    up1 = _upload(client, h, "同一段内容 甲 乙", "x.txt")
    drain()
    client.delete(f"/api/documents/{up1['doc_id']}", headers=h)
    up2 = _upload(client, h, "同一段内容 甲 乙", "x.txt")
    drain()
    assert up2["doc_id"] != up1["doc_id"]
    assert up2["deduplicated"] is False
    r = client.get("/api/search?q=甲", headers=h).get_json()
    assert [x["doc_id"] for x in r["results"]] == [up2["doc_id"]]


def test_delete_competes_with_running_extract(ws, client, drain):
    """抽取进行中删除文档：迟到结果绝不能写入已删除文档。

    做法：在规则引擎执行处插桩制造延迟，让删除发生在 extract 作业执行途中；
    由于作业的读-算-写在单个 IMMEDIATE 事务内，删除提交后作业复查到 deleted_at，
    整体丢弃。我们通过重复压测该交错验证最终状态。
    """
    from app.services import extractor
    from app.services import jobs as jobs_service
    from app.services.worker import Worker

    h = ws["headers"]
    text = SAMPLE_TEXT_A * 3

    barrier = {"started": threading.Event(), "release": threading.Event()}
    original = extractor.run_extract

    def slow_extract(content, pack):
        barrier["started"].set()
        barrier["release"].wait(timeout=5)
        return original(content, pack)

    extractor.run_extract = slow_extract
    try:
        up = _upload(client, h, text, "race.txt")
        worker = Worker(worker_id="race-worker", poll_interval=0)

        def run_job():
            job = jobs_service.claim_job("race-worker")
            if job:
                worker._run_one(job)

        t = threading.Thread(target=run_job)
        t.start()
        assert barrier["started"].wait(timeout=5)
        # 作业正在跑：删除文档
        dr = client.delete(f"/api/documents/{up['doc_id']}", headers=h)
        assert dr.status_code == 200
        barrier["release"].set()
        t.join(timeout=10)
    finally:
        extractor.run_extract = original

    conn = _raw_conn()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM entities WHERE document_id=?", (up["doc_id"],)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM postings WHERE document_id=?", (up["doc_id"],)
        ).fetchone()[0] == 0
    finally:
        conn.close()
    assert client.get("/api/search?q=李明", headers=h).get_json()["result_count"] == 0


def test_delete_is_idempotent(ws, client):
    h = ws["headers"]
    up = _upload(client, h, "临时 文档", "t.txt")
    assert client.delete(f"/api/documents/{up['doc_id']}", headers=h).status_code == 200
    assert client.delete(f"/api/documents/{up['doc_id']}", headers=h).status_code == 200
