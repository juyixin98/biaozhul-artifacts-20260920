"""End-to-end HTTP tests through the FastAPI app.

The module-level service is swapped for one backed by an in-memory SQLite
store; the inference core exercised here is the real engine, not a stub.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.api import server
from app.service import DatalogService
from app.store.sqlite_store import EvidenceStore

from .fixtures_programs import ANCESTOR_PROGRAM, GRAPH_PROGRAM, INVALID_PROGRAMS


@pytest.fixture
def client():
    mem_store = EvidenceStore(":memory:")
    svc = DatalogService(mem_store)
    old_service = server.service
    old_store = server.store
    server.service = svc
    server.store = mem_store
    with TestClient(server.app) as c:
        yield c
    server.service = old_service
    server.store = old_store
    mem_store.close()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_query_endpoint_ancestor(client):
    r = client.post(
        "/query",
        json={"program": ANCESTOR_PROGRAM, "goal": "ancestor(ann, X)", "request_id": "http-1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["request_id"] == "http-1"
    assert body["version"].startswith("sha256:")
    xs = sorted(a["bindings"]["X"] for a in body["answers"])
    assert xs == ["ben", "bob", "cy", "dan"]
    # answers carry proof and key steps/version are surfaced
    assert body["answers"][0]["proof"]["kind"] == "rule"
    assert body["strata"][0]["predicates"] == ["ancestor/2"]
    assert body["steps"]


def test_query_endpoint_negation(client):
    r = client.post("/query", json={"program": GRAPH_PROGRAM, "goal": "unreachable(X)"})
    body = r.json()
    assert body["status"] == "ok"
    assert sorted(a["bindings"]["X"] for a in body["answers"]) == ["a", "d"]


def test_query_unsafe_program_returns_typed_failure(client):
    r = client.post(
        "/query",
        json={"program": INVALID_PROGRAMS["unsafe_head"], "goal": "p(X)", "request_id": "http-bad"},
    )
    body = r.json()
    assert body["status"] == "error"
    assert body["failures"] == [
        {"category": "unsafe_variable", "message": body["failures"][0]["message"]}
    ]
    assert "not bound" in body["failures"][0]["message"]


def test_query_negation_cycle_returns_typed_failure(client):
    r = client.post(
        "/query", json={"program": INVALID_PROGRAMS["negation_cycle"], "goal": "p(X)"}
    )
    assert r.json()["failures"][0]["category"] == "negation_cycle"


def test_compile_endpoint_reports_strata(client):
    r = client.post("/compile", json={"program": GRAPH_PROGRAM})
    body = r.json()
    assert body["status"] == "ok"
    levels = {p for s in body["strata"] for p in s["predicates"]}
    assert {"reachable/1", "sink/1", "unreachable/1"} <= levels


def test_materialize_persists_and_summary_reads_back(client):
    r = client.post("/materialize", json={"program": ANCESTOR_PROGRAM, "request_id": "mat-1"})
    assert r.json()["status"] == "ok"
    s = client.get("/requests/mat-1/summary").json()
    counts = {row["predicate"]: row["count"] for row in s["relations"]}
    assert counts["ancestor/2"] == 12
    assert counts["parent/2"] == 5

    # derive a single tuple's SQL support
    enc = json.dumps(["ann", "cy"])
    d = client.get(f"/requests/mat-1/derivation", params={"pred": "ancestor", "args": enc})
    assert d.status_code == 200
    assert d.json()["kind"] == "idb"
    assert any(row["kind"] == "pos" for row in d.json()["support"])


def test_missing_request_is_404(client):
    r = client.get("/requests/nope")
    assert r.status_code == 404
    assert r.json()["failures"][0]["category"] == "unknown_request"


def test_derivation_bad_args_is_400(client):
    client.post("/materialize", json={"program": ANCESTOR_PROGRAM, "request_id": "mat-2"})
    r = client.get("/requests/mat-2/derivation", params={"pred": "ancestor", "args": "notjson"})
    assert r.status_code == 400
    assert r.json()["failures"][0]["category"] == "bad_request"


def test_malformed_json_is_422(client):
    r = client.post("/query", json={"program": ANCESTOR_PROGRAM})  # goal missing
    assert r.status_code == 422
