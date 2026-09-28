"""HTTP-level integration tests with a real ASGI app + SQLite on disk.

These exercise the full stack: fixture file -> build -> FastAPI -> SQLite ->
query response, asserting concrete JSON results, concrete error categories,
and explainable traces (request id, steps, version, uncertainties).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from collsvc.api.app import create_app
from collsvc.config import Settings

PROJECT = Path(__file__).resolve().parents[2]
DEMO = PROJECT / "data" / "corpus" / "demo.json"


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        db_path=tmp_path / "data" / "collsvc.db",
        corpus_dir=PROJECT / "data" / "corpus",
        page_size_default=50,
        page_size_max=200,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


EN_T3 = {"locale": "en_US", "strength": 3, "numeric": False, "case_first": "default"}
EN_T3_NUM = {**EN_T3, "numeric": True}
TR_T3 = {**EN_T3, "locale": "tr_TR"}


def _build(client, name="demo", options=EN_T3, replace=True):
    resp = client.post(
        "/admin/index/build",
        json={"corpus_name": name, "options": options, "replace": replace},
    )
    return resp


def test_health_reports_icu_versions(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["icu_version"] == "74.2"
    assert body["unicode_version"] == "15.1"


def test_build_then_sorted_concrete(client):
    assert _build(client).status_code == 200
    resp = client.post("/query/sorted", json={"options": EN_T3, "limit": 100})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    texts = [r["text"] for r in body["rows"]]
    # Concrete: accents order cote < coté < côte < côté, and NFC+NFD both present.
    cotes = [t for t in texts if t in {"cote", "coté", "côte", "côté"}]
    assert cotes[0] == "cote"
    assert "côté" in texts and "côté" in texts  # NFC and NFD spellings
    assert body["index_version"].startswith("idx_v1_")
    assert body["trace"]["request_id"]
    assert body["trace"]["steps"], "trace must record ordered steps"


def test_query_before_build_is_index_not_built(client):
    resp = client.post("/query/sorted", json={"options": EN_T3})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_category"] == "INDEX_NOT_BUILT"
    assert body["trace"]["failures"][0]["category"] == "INDEX_NOT_BUILT"


def test_build_unknown_corpus_is_404(client):
    resp = _build(client, name="does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error_category"] == "CORPUS_NOT_FOUND"


def test_unsupported_locale_rejected(client):
    resp = _build(client, options={**EN_T3, "locale": "xx_YY"})
    assert resp.status_code == 400
    assert resp.json()["error_category"] == "UNSUPPORTED_LOCALE"


def test_invalid_strength_rejected(client):
    resp = client.post(
        "/query/range",
        json={"options": {**EN_T3, "strength": 7}, "low": "a", "high": "z"},
    )
    assert resp.status_code == 400
    assert resp.json()["error_category"] == "INVALID_ARGUMENT"


def test_range_uses_sort_keys_not_utf8(client):
    _build(client)
    resp = client.post(
        "/query/range", json={"options": EN_T3, "low": "e", "high": "f", "limit": 100}
    )
    body = resp.json()
    texts = {r["text"] for r in body["rows"]}
    assert {"étude", "élève"} <= texts
    # Trace documents the exact key-level bounds used.
    bounds = next(
        s for s in body["trace"]["steps"] if s["name"] == "compute_key_bounds"
    )
    assert bounds["detail"]["low_key"] and bounds["detail"]["high_key"]


def test_collation_and_text_prefix_differ_concretely(client):
    _build(client, options={**EN_T3, "strength": 1})
    # Collation prefix at PRIMARY ignores case+accents.
    coll = client.post(
        "/query/prefix",
        json={"options": {**EN_T3, "strength": 1}, "prefix": "COTE",
              "match": "collation", "limit": 100},
    ).json()
    coll_texts = {r["text"] for r in coll["rows"]}
    assert {"cote", "coté", "côte", "côté"} <= coll_texts

    # Text prefix on NFC text is exact: COTE matches nothing lowercase.
    txt = client.post(
        "/query/prefix",
        json={"options": {**EN_T3, "strength": 1}, "prefix": "COTE",
              "match": "text", "limit": 100},
    ).json()
    assert txt["rows"] == []


def test_numeric_prefix_flags_degradation(client):
    _build(client, options=EN_T3_NUM)
    resp = client.post(
        "/query/prefix",
        json={"options": EN_T3_NUM, "prefix": "file", "match": "collation",
              "limit": 100},
    )
    body = resp.json()
    assert [r["text"] for r in body["rows"]] == [
        "file1", "file2", "file02", "file10", "file20"
    ]
    assert body["degraded"] is True
    reasons = {u["detail"]["reason"] for u in body["trace"]["uncertainties"]}
    assert "numeric_collation_merges_digit_weights" in reasons


def test_sorted_numeric_ordering(client):
    _build(client, options=EN_T3_NUM)
    body = client.post(
        "/query/sorted", json={"options": EN_T3_NUM, "limit": 100}
    ).json()
    files = [r["text"] for r in body["rows"] if r["text"].startswith("file")]
    assert files == ["file1", "file2", "file02", "file10", "file20"]


def test_turkish_tailoring_over_http(client):
    _build(client, options=TR_T3)
    body = client.post("/query/sorted", json={"options": TR_T3, "limit": 100}).json()
    tr = [r["text"] for r in body["rows"] if r["doc_id"].startswith("tr-")]
    assert tr == ["ağır", "çay", "ırmak", "İstanbul", "öğle",
                  "sıcak", "şapka", "üç"]


def test_turkish_and_english_have_different_index_versions(client):
    _build(client, options=EN_T3)
    _build(client, options=TR_T3)
    en_meta = client.get(
        "/admin/index/meta", params={"locale": "en_US", "strength": 3}
    ).json()
    tr_meta = client.get(
        "/admin/index/meta", params={"locale": "tr_TR", "strength": 3}
    ).json()
    assert en_meta["meta"]["index_version"] != tr_meta["meta"]["index_version"]


def test_pagination_over_http_is_complete(client):
    _build(client)
    seen, cursor = [], None
    for _ in range(20):
        body = client.post(
            "/query/sorted",
            json={"options": EN_T3, "limit": 6, "cursor": cursor},
        ).json()
        seen.extend(r["doc_id"] for r in body["rows"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    full = client.post(
        "/query/sorted", json={"options": EN_T3, "limit": 200}
    ).json()
    assert seen == [r["doc_id"] for r in full["rows"]]
    assert len(seen) == 35


def test_stale_cursor_after_rebuild_is_version_conflict(client):
    _build(client, options=EN_T3)
    page = client.post(
        "/query/sorted", json={"options": EN_T3, "limit": 3}
    ).json()
    old_cursor = page["next_cursor"]

    # Rebuild under changed strength -> new version.
    _build(client, options={**EN_T3, "strength": 2})
    resp = client.post(
        "/query/sorted",
        json={"options": {**EN_T3, "strength": 2}, "limit": 3, "cursor": old_cursor},
    )
    assert resp.status_code == 409
    assert resp.json()["error_category"] == "INDEX_VERSION_CONFLICT"


def test_cursor_cannot_cross_modes(client):
    _build(client)
    page = client.post("/query/sorted", json={"options": EN_T3, "limit": 3}).json()
    resp = client.post(
        "/query/range",
        json={"options": EN_T3, "low": "a", "high": "z", "limit": 3,
              "cursor": page["next_cursor"]},
    )
    assert resp.status_code == 400
    assert resp.json()["error_category"] == "INVALID_CURSOR"


def test_meta_reflects_build(client):
    _build(client)
    resp = client.get("/admin/index/meta")
    body = resp.json()
    assert body["success"] is True
    assert body["meta"]["row_count"] == "35"
    assert body["meta"]["actual_locale"] == "en_US"
    assert body["meta"]["icu_version"] == "74.2"


def test_trace_carries_request_id_and_steps(client):
    _build(client)
    body = client.post(
        "/query/range",
        json={"options": EN_T3, "low": "e", "high": "f", "limit": 5},
    ).json()
    trace = body["trace"]
    assert len(trace["request_id"]) >= 8
    names = [s["name"] for s in trace["steps"]]
    assert names == ["compute_key_bounds", "return_page"]
    assert trace["failures"] == []
