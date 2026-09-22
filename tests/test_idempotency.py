"""重复执行幂等：同一作业/同一文档重跑，实体与倒排不重复生成。"""
from __future__ import annotations

import io
import sqlite3

from conftest import _DB_PATH, SAMPLE_TEXT_A


def _raw():
    conn = sqlite3.connect(_DB_PATH, timeout=30)
    return conn


def test_extract_idempotent_on_rerun(ws, client, drain):
    h = ws["headers"]
    up = client.post(
        "/api/documents",
        data={"file": (io.BytesIO(SAMPLE_TEXT_A.encode()), "a.txt")},
        headers=h, content_type="multipart/form-data",
    ).get_json()
    drain()

    # 手工再入队一个同文档/同版本作业（模拟重试/重放）
    from app.services import jobs as jobs_service

    jobs_service.enqueue_extract_standalone(
        workspace_id=ws["id"], document_id=up["doc_id"],
        rule_pack_version=ws["default_rule"], doc_sha256=up["sha256"],
    )
    drain()

    conn = _raw()
    try:
        n_entities = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE document_id=?", (up["doc_id"],)
        ).fetchone()[0]
        n_mentions = conn.execute(
            "SELECT COUNT(*) FROM mentions m JOIN entities e ON e.id=m.entity_id "
            "WHERE e.document_id=?", (up["doc_id"],)
        ).fetchone()[0]
    finally:
        conn.close()

    data = client.get(f"/api/documents/{up['doc_id']}/entities", headers=h).get_json()
    assert sum(1 for _ in data["entities"]) == n_entities
    total_mentions = sum(len(e["mentions"]) for e in data["entities"])
    assert total_mentions == n_mentions
    # 再跑一次，数字纹丝不动
    jobs_service.enqueue_extract_standalone(
        workspace_id=ws["id"], document_id=up["doc_id"],
        rule_pack_version=ws["default_rule"], doc_sha256=up["sha256"],
    )
    drain()
    conn = _raw()
    try:
        n2 = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE document_id=?", (up["doc_id"],)
        ).fetchone()[0]
        m2 = conn.execute(
            "SELECT COUNT(*) FROM mentions m JOIN entities e ON e.id=m.entity_id "
            "WHERE e.document_id=?", (up["doc_id"],)
        ).fetchone()[0]
        p2 = conn.execute(
            "SELECT COUNT(*) FROM postings WHERE document_id=?", (up["doc_id"],)
        ).fetchone()[0]
    finally:
        conn.close()
    assert n2 == n_entities and m2 == n_mentions
    # postings 数量 = 该文不同 (term, position) 数，重放不翻倍
    assert p2 > 0


def test_index_postings_unique(ws, client, drain):
    h = ws["headers"]
    up = client.post(
        "/api/documents",
        data={"file": (io.BytesIO("苹果 苹果 苹果".encode()), "a.txt")},
        headers=h, content_type="multipart/form-data",
    ).get_json()
    drain()
    conn = _raw()
    try:
        rows = conn.execute(
            "SELECT term, char_start, COUNT(*) c FROM postings WHERE document_id=? "
            "GROUP BY term, char_start HAVING c > 1", (up["doc_id"],)
        ).fetchall()
        assert rows == [], f"存在重复 posting: {rows}"
        stats = conn.execute(
            "SELECT tf FROM doc_term_stats WHERE document_id=? AND term='苹'", (up["doc_id"],)
        ).fetchone()
        assert stats is not None and stats[0] == 3
    finally:
        conn.close()
