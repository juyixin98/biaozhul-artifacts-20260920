"""端到端集成：上传 → 作业 → 四类实体 → 搜索 → 关键词。"""
from __future__ import annotations

from conftest import SAMPLE_TEXT_A, SAMPLE_TEXT_B


def _upload(client, headers, text, name="doc.txt"):
    resp = client.post(
        "/api/documents",
        data={"file": (__import__("io").BytesIO(text.encode("utf-8")), name)},
        headers=headers,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201, resp.data
    return resp.get_json()


def _wait_jobs(client, headers, expected_extracts=1, timeout=10):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get("/api/jobs", headers=headers).get_json()["jobs"]
        pending = [j for j in r if j["status"] in ("pending", "running")]
        failed = [j for j in r if j["status"] == "failed"]
        assert not failed, f"有作业失败: {failed}"
        if len([j for j in r if j["kind"] == "extract" and j["status"] == "succeeded"]) >= expected_extracts \
                and not pending:
            return r
        time.sleep(0.05)
    raise AssertionError("作业未在超时内完成")


def test_health(client):
    assert client.get("/healthz").status_code == 200


def test_upload_extract_and_search(ws, client, drain):
    h = ws["headers"]
    up = _upload(client, h, SAMPLE_TEXT_A, "news-a.txt")
    assert up["deduplicated"] is False
    assert up["job_id"] > 0

    drain()
    _wait_jobs(client, h, expected_extracts=1)

    # 四类实体都被真实识别（不是空，也不是固定返回）
    ents_resp = client.get(f"/api/documents/{up['doc_id']}/entities", headers=h)
    assert ents_resp.status_code == 200
    ents = ents_resp.get_json()["entities"]
    by_type = {}
    for e in ents:
        by_type.setdefault(e["entity_type"], []).append(e)

    assert {"person", "org", "tech", "date"} <= set(by_type), by_type.keys()

    persons = {e["canonical"] for e in by_type["person"]}
    assert "李明" in persons
    assert "Alan Kay" in persons

    orgs = {e["canonical"] for e in by_type["org"]}
    assert "北京大学" in orgs
    assert "阿里巴巴集团" in orgs

    techs = {e["canonical"] for e in by_type["tech"]}
    assert "Python 3.12.1" in techs
    assert "SQLite" in techs

    dates = {e["canonical"] for e in by_type["date"]}
    assert "2024-03-15" in dates
    assert "2024-09-02" in dates

    # 搜索：AND 语义——A 文同时含 Python 和 SQLite，应命中
    s = client.get("/api/search?q=Python SQLite&top_k=5", headers=h).get_json()
    assert s["result_count"] == 1
    assert s["results"][0]["doc_id"] == up["doc_id"]

    s = client.get("/api/search?q=Python", headers=h).get_json()
    assert s["result_count"] == 1
    assert s["results"][0]["doc_id"] == up["doc_id"]
    assert s["results"][0]["positions"]["python"], "posting 位置缺失"

    # 含一个词和一个不相干词 → AND 不命中
    s_none = client.get("/api/search?q=Python 量子隧穿", headers=h).get_json()
    assert s_none["result_count"] == 0


def test_search_and_chinese(ws, client, drain):
    h = ws["headers"]
    _upload(client, h, SAMPLE_TEXT_A, "a.txt")
    drain()
    # 多字中文：逐字 AND，命中原文含连续词的文档
    s = client.get("/api/search?q=北京大学", headers=h).get_json()
    assert s["result_count"] == 1
    # 不相关的字组合不命中
    s2 = client.get("/api/search?q=" + "企鹅航天", headers=h).get_json()
    assert s2["result_count"] == 0


def test_keywords_are_tfidf_and_deterministic(ws, client, drain):
    h = ws["headers"]
    up = _upload(client, h, SAMPLE_TEXT_A, "a.txt")
    _upload(client, h, SAMPLE_TEXT_B, "b.txt")
    drain()

    kw1 = client.get(f"/api/documents/{up['doc_id']}/keywords?top_k=8", headers=h).get_json()
    kw2 = client.get(f"/api/documents/{up['doc_id']}/keywords?top_k=8", headers=h).get_json()
    assert kw1 == kw2, "关键词必须确定性"
    terms = [k["term"] for k in kw1["keywords"]]
    assert len(terms) == len(set(terms))
    weights = [k["weight"] for k in kw1["keywords"]]
    assert weights == sorted(weights, reverse=True), "必须按权重降序"


def test_search_stable_ordering(ws, client, drain):
    h = ws["headers"]
    ids = []
    for i in range(3):
        up = _upload(client, h, f"重复词 苹果 苹果 苹果 unique{i}", f"d{i}.txt")
        ids.append(up["doc_id"])
    drain()
    r1 = client.get("/api/search?q=苹果&top_k=10", headers=h).get_json()
    r2 = client.get("/api/search?q=苹果&top_k=10", headers=h).get_json()
    assert [r["doc_id"] for r in r1["results"]] == [r["doc_id"] for r in r2["results"]]
    # doc_id 升序兜底稳定
    assert [r["doc_id"] for r in r1["results"]] == sorted(r["doc_id"] for r in r1["results"])


def test_upload_rejects_non_utf8(ws, client):
    h = ws["headers"]
    resp = client.post(
        "/api/documents",
        data={"file": (__import__("io").BytesIO(b"\xff\xfe not utf8"), "bad.txt")},
        headers=h,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "UTF-8" in resp.get_json()["message"]
