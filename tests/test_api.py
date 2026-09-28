"""End-to-end API tests: build, batch query, failure categories, audit trail."""

import pytest
from fastapi.testclient import TestClient

from conftest import b64
from lcs_batch.api import create_app
from lcs_batch.config import KERNEL_VERSION


@pytest.fixture()
def client(settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def built_client(client):
    response = client.post(
        "/v1/index/build",
        json={
            "documents": [
                {"doc_id": "alpha", "content_b64": b64(b"abcXYZdef")},
                {"doc_id": "beta", "content_b64": b64(b"abcQQdef")},
                {"doc_id": "gamma", "content_b64": b64(b"rbd")},
            ]
        },
    )
    assert response.status_code == 200, response.text
    return client


def test_health(client):
    body = client.get("/health").json()
    assert body == {"status": "ok", "kernel_version": KERNEL_VERSION}


def test_build_reports_request_identity_and_version(client):
    response = client.post(
        "/v1/index/build",
        json={"documents": [{"doc_id": "a", "content_b64": b64(b"hello")}]},
        headers={"X-Request-ID": "req-build-1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == "req-build-1"
    assert body["index_version"] == KERNEL_VERSION
    assert body["doc_count"] == 1
    assert response.headers["x-request-id"] == "req-build-1"


def test_index_info_after_build(built_client):
    info = built_client.get("/v1/index/info").json()
    assert info["doc_count"] == 3
    assert info["kernel_version"] == KERNEL_VERSION
    assert [d["doc_id"] for d in info["documents"]] == ["alpha", "beta", "gamma"]


def test_query_before_build_returns_typed_404(client):
    response = client.post("/v1/query", json={"queries": [{"min_docs": 2}]})
    assert response.status_code == 404
    assert response.json()["error"]["category"] == "INDEX_NOT_FOUND"


def test_batch_query_ok_and_error_items_separated(built_client):
    response = built_client.post(
        "/v1/query",
        json={
            "request_id": "req-batch-1",
            "queries": [
                {"query_id": "all3", "min_docs": 3},
                {"query_id": "pair", "min_docs": 2},
                {"query_id": "bad", "min_docs": 1},
                {"query_id": "too-many", "min_docs": 9},
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == "req-batch-1"
    assert body["index_version"] == KERNEL_VERSION

    items = {item["query_id"]: item for item in body["items"]}
    assert items["all3"]["status"] == "ok"
    assert items["pair"]["status"] == "ok"
    assert items["bad"]["status"] == "error"
    assert items["bad"]["error"]["category"] == "INVALID_MIN_DOCS"
    assert items["too-many"]["error"]["category"] == "MIN_DOCS_EXCEEDS_CORPUS"

    # failures are also listed separately for review
    assert {f["category"] for f in body["failures"]} == {
        "INVALID_MIN_DOCS",
        "MIN_DOCS_EXCEEDS_CORPUS",
    }

    # min_docs=3: "b" and "d" (length 1) are shared by alpha, beta, gamma
    all3 = items["all3"]["result"]
    assert all3["max_length"] == 1
    assert [c["substring_hex"] for c in all3["candidates"]] == [
        b"b".hex(),
        b"d".hex(),
    ]
    assert all3["candidates"][0]["doc_coverage"] == ["alpha", "beta", "gamma"]

    # min_docs=2: tied "abc" and "def" (alpha/beta), stable byte order
    pair = items["pair"]["result"]
    assert pair["max_length"] == 3
    assert [c["substring_hex"] for c in pair["candidates"]] == [
        b"abc".hex(),
        b"def".hex(),
    ]
    abc = pair["candidates"][0]
    assert abc["doc_coverage"] == ["alpha", "beta"]
    assert abc["occurrences"] == [
        {"doc_id": "alpha", "offsets": [0]},
        {"doc_id": "beta", "offsets": [0]},
    ]


def test_truncation_surfaces_as_uncertainty(built_client):
    body = built_client.post(
        "/v1/query",
        json={"queries": [{"query_id": "t", "min_docs": 2, "max_candidates": 1}]},
    ).json()
    item = body["items"][0]
    assert item["status"] == "ok"
    assert item["result"]["truncated"] is True
    assert item["result"]["candidate_count"] == 1
    assert item["warnings"], "truncation must be flagged as a warning"
    assert body["uncertainties"] == [
        {"query_id": "t", "warning": item["warnings"][0]}
    ]


def test_no_common_substring_is_a_warning_not_an_error(built_client):
    built_client.post(
        "/v1/index/build",
        json={
            "documents": [
                {"doc_id": "x", "content_b64": b64(b"aaa")},
                {"doc_id": "y", "content_b64": b64(b"bbb")},
            ]
        },
    )
    body = built_client.post(
        "/v1/query", json={"queries": [{"query_id": "none", "min_docs": 2}]}
    ).json()
    item = body["items"][0]
    assert item["status"] == "ok"
    assert item["result"]["max_length"] == 0
    assert item["warnings"]
    assert body["uncertainties"]


def test_build_validation_error_category(client):
    response = client.post(
        "/v1/index/build",
        json={
            "documents": [
                {"doc_id": "dup", "content_b64": b64(b"a")},
                {"doc_id": "dup", "content_b64": b64(b"b")},
            ]
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["category"] == "DUPLICATE_DOC_ID"


def test_audit_log_records_request_identity(built_client):
    built_client.post(
        "/v1/query",
        json={
            "request_id": "req-audit-1",
            "queries": [
                {"query_id": "ok1", "min_docs": 2},
                {"query_id": "bad1", "min_docs": 0},
            ],
        },
    )
    entries = built_client.get("/v1/audit/recent").json()["entries"]
    by_query = {e["query_id"]: e for e in entries}
    assert by_query["ok1"]["request_id"] == "req-audit-1"
    assert by_query["ok1"]["status"] == "ok"
    assert by_query["bad1"]["status"] == "error"
    assert "INVALID_MIN_DOCS" in by_query["bad1"]["error_json"]


def test_index_survives_service_restart(settings):
    app1 = create_app(settings)
    with TestClient(app1) as c1:
        c1.post(
            "/v1/index/build",
            json={
                "documents": [
                    {"doc_id": "a", "content_b64": b64(b"common-part")},
                    {"doc_id": "b", "content_b64": b64(b"xxcommon-partxx")},
                ]
            },
        )
    # a fresh service over the same database loads the persisted index
    app2 = create_app(settings)
    with TestClient(app2) as c2:
        body = c2.post(
            "/v1/query", json={"queries": [{"query_id": "q", "min_docs": 2}]}
        ).json()
    result = body["items"][0]["result"]
    assert result["max_length"] == len(b"common-part")
    assert result["candidates"][0]["substring_hex"] == b"common-part".hex()
