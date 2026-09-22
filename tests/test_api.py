"""End-to-end HTTP workflow tests."""
from __future__ import annotations

import json

from .conftest import auth, create_ws, drain


def test_health_reports_wal(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["journal_mode"].lower() == "wal"


def test_workspace_creation_returns_key_once(client):
    ws_id, key = create_ws(client, "ws1")
    # Key is never returned on subsequent management calls.
    resp = client.get(f"/api/workspaces/{ws_id}/documents")
    assert resp.status_code == 401
    resp = client.get(f"/api/workspaces/{ws_id}/documents", headers=auth(ws_id, key))
    assert resp.status_code == 200


def test_wrong_key_rejected(client):
    ws_id, _ = create_ws(client, "ws-secret")
    resp = client.get(f"/api/workspaces/{ws_id}/documents",
                      headers=auth(ws_id, "nope"))
    assert resp.status_code == 401


def test_upload_non_utf8_rejected(client):
    ws_id, key = create_ws(client, "ws-utf8")
    resp = client.post(
        f"/api/workspaces/{ws_id}/documents",
        data=b"\xff\xfe\x00bad",
        headers={**auth(ws_id, key), "Content-Type": "text/plain"},
    )
    assert resp.status_code == 400


def test_full_extraction_workflow_with_real_offsets(client):
    ws_id, key = create_ws(client, "ws-flow")
    h = auth(ws_id, key)
    text = "小李和Alice在2023-05-01评审了Kafka 3.6，老张做记录。"
    resp = client.post(
        f"/api/workspaces/{ws_id}/documents",
        json={"text": text, "title": "review.txt"},
        headers=h,
    )
    assert resp.status_code == 202
    doc_id = resp.get_json()["document_id"]

    drain()

    resp = client.get(
        f"/api/workspaces/{ws_id}/entities?document_id={doc_id}", headers=h
    )
    ents = resp.get_json()["entities"]
    types = {e["entity_type"] for e in ents}
    assert {"PERSON", "DATE", "TECH"} <= types

    # Every mention offsets back into the original text and explains itself.
    for e in ents:
        assert text[e["start_char"]:e["end_char"]] == e["text"]
        assert e["matched_rule"]

    # Alias normalization preserves the original mention.
    xiaoli = next(e for e in ents if e["text"] == "小李")
    assert xiaoli["canonical_name"] == "李明"
    laozhang = next(e for e in ents if e["text"] == "老张")
    assert laozhang["canonical_name"] == "张伟"

    # Grouped view keeps all surface forms under one canonical name.
    grouped = client.get(f"/api/workspaces/{ws_id}/entities/grouped", headers=h)
    persons = grouped.get_json()["groups"]["PERSON"]
    mentions = {m["text"] for m in persons["李明"]}
    assert "小李" in mentions


def test_content_dedup_reuses_blob_within_workspace(client):
    ws_id, key = create_ws(client, "ws-dedup")
    h = auth(ws_id, key)
    payload = {"text": "清华大学与Kafka的故事", "title": "a"}
    r1 = client.post(f"/api/workspaces/{ws_id}/documents", json=payload, headers=h)
    r2 = client.post(f"/api/workspaces/{ws_id}/documents", json=payload, headers=h)
    assert r1.status_code == 202
    assert r2.status_code == 200
    assert r2.get_json()["deduplicated"] is True
    drain()
    # Entities exist exactly once despite duplicate upload.
    ents = client.get(f"/api/workspaces/{ws_id}/entities", headers=h).get_json()
    assert ents["total"] == len({(e["document_id"], e["start_char"], e["entity_type"])
                                 for e in ents["entities"]})


def test_search_tfidf_and_keywords(client):
    ws_id, key = create_ws(client, "ws-search")
    h = auth(ws_id, key)
    docs = [
        "Kafka 分布式消息队列的架构与部署",
        "PostgreSQL 数据库的索引与查询优化",
        "Kafka 与 PostgreSQL 的数据管道实践",
    ]
    for i, text in enumerate(docs):
        client.post(f"/api/workspaces/{ws_id}/documents",
                    json={"text": text, "title": f"d{i}"}, headers=h)
    drain()

    resp = client.get(f"/api/workspaces/{ws_id}/search?q=Kafka", headers=h)
    body = resp.get_json()
    titles = [r["title"] for r in body["results"]]
    assert body["total"] == 2
    assert set(titles) == {"d0", "d2"}
    # Deterministic tie behavior: equal scores ordered by document_id asc.
    ids = [r["document_id"] for r in body["results"]]
    assert ids == sorted(ids)
    assert "index_generation" in body

    kw = client.get(
        f"/api/workspaces/{ws_id}/documents/1/keywords?top_k=5", headers=h
    ).get_json()
    terms = [k["term"] for k in kw["keywords"]]
    assert "kafka" in terms
    weights = [k["weight"] for k in kw["keywords"]]
    assert weights == sorted(weights, reverse=True)  # stable desc ordering


def test_export_is_ndjson(client):
    ws_id, key = create_ws(client, "ws-export")
    h = auth(ws_id, key)
    client.post(f"/api/workspaces/{ws_id}/documents",
                json={"text": "李明在2020-01-01加入联合国", "title": "x"}, headers=h)
    drain()
    resp = client.get(f"/api/workspaces/{ws_id}/export", headers=h)
    assert resp.mimetype.startswith("application/x-ndjson")
    records = [json.loads(line) for line in resp.data.decode("utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["entities"]
    assert all("matched_rule" in e for e in records[0]["entities"])


def test_search_empty_query(client):
    ws_id, key = create_ws(client, "ws-emptyq")
    resp = client.get("/api/workspaces/%d/search?q=" % ws_id, headers=auth(ws_id, key))
    assert resp.get_json()["results"] == []


def test_pagination_limits(client):
    ws_id, key = create_ws(client, "ws-page")
    h = auth(ws_id, key)
    for i in range(3):
        client.post(f"/api/workspaces/{ws_id}/documents",
                    json={"text": f"独立文档编号{i} 内容", "title": f"p{i}"}, headers=h)
    page1 = client.get(f"/api/workspaces/{ws_id}/documents?limit=2&offset=0",
                       headers=h).get_json()
    page2 = client.get(f"/api/workspaces/{ws_id}/documents?limit=2&offset=2",
                       headers=h).get_json()
    assert page1["total"] == 3 and len(page1["documents"]) == 2
    assert len(page2["documents"]) == 1
