"""End-to-end HTTP tests via FastAPI's TestClient (in-process, real ASGI)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from provenance.api import create_app
from provenance.config import Settings

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "graph_v1.json"
).read_text()

TWO_HOP = {
    "op": "project",
    "columns": ["x.src", "y.dst"],
    "child": {
        "op": "join",
        "left": {"op": "relation", "name": "edge", "version": "v1", "alias": "x"},
        "right": {"op": "relation", "name": "edge", "version": "v1", "alias": "y"},
        "predicates": [{"op": "=", "left": "x.dst", "right": "y.src"}],
    },
}


@pytest.fixture
def client(tmp_path):
    settings = Settings(db_path=tmp_path / "api.db", fixture_dir=tmp_path)
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def _load(client) -> dict:
    response = client.post("/v1/snapshots", json={"snapshot": json.loads(FIXTURE)})
    assert response.status_code == 200, response.text
    return response.json()


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_load_snapshot_then_query(client):
    _load(client)
    response = client.post("/v1/provenance/query", json={"request_id": "r1", "plan": TWO_HOP})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["request_id"] == "r1"
    assert body["version"] == "v1"
    outputs = {tuple(sorted(row["output"].items())): row for row in body["rows"]}
    # Find the self-loop row and assert x**2.
    squared = next(row for row in body["rows"] if row["output"] == {"src": "d", "dst": "d"})
    assert squared["provenance"] == "edge.e6**2"
    assert squared["multiplicity"] == 1


def test_query_with_weight_verification_agrees(client):
    _load(client)
    weights = {f"edge.e{i}": p for i, p in enumerate([2, 3, 5, 7, 11, 13], start=1)}
    response = client.post(
        "/v1/provenance/query",
        json={"request_id": "r2", "plan": TWO_HOP, "weights": weights},
    )
    assert response.status_code == 200, response.text
    verification = response.json()["weight_verification"]
    assert verification["agreed"] is True
    assert verification["mismatches"] == []
    assert verification["checked_outputs"] == 4


def test_read_back_same_version(client):
    _load(client)
    client.post("/v1/provenance/query", json={"request_id": "r3", "plan": TWO_HOP})
    response = client.get("/v1/runs/r3")
    assert response.status_code == 200
    record = response.json()
    assert record["versions"] == ["v1"]
    assert record["status"] == "completed"
    assert len(record["rows"]) == 4


def test_malformed_plan_reports_failure_category(client):
    _load(client)
    response = client.post(
        "/v1/provenance/query",
        json={"plan": {"op": "difference", "left": {}, "right": {}}},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert body["category"] == "rejected_plan"
    assert body["request_id"]  # every rejection is correlated


def test_missing_version_is_422_category(client):
    _load(client)
    plan = {"op": "relation", "name": "edge", "version": "does-not-exist"}
    response = client.post("/v1/provenance/query", json={"plan": plan})
    assert response.status_code == 422
    assert response.json()["category"] == "rejected_input_version"


def test_mixed_versions_rejected_over_http(client):
    _load(client)
    client.post(
        "/v1/snapshots",
        json={
            "snapshot": {
                "version": "v2",
                "relations": {
                    "edge": {
                        "columns": ["src", "dst"],
                        "rows": [{"id": "z", "data": {"src": "q", "dst": "r"}}],
                    }
                },
            }
        },
    )
    plan = {
        "op": "join",
        "left": {"op": "relation", "name": "edge", "version": "v1", "alias": "x"},
        "right": {"op": "relation", "name": "edge", "version": "v2", "alias": "y"},
        "predicates": [{"op": "=", "left": "x.dst", "right": "y.src"}],
    }
    response = client.post("/v1/provenance/query", json={"plan": plan})
    assert response.status_code == 422
    assert response.json()["category"] == "rejected_input_version"


def test_bad_weights_rejected(client):
    _load(client)
    response = client.post(
        "/v1/provenance/query",
        json={"request_id": "r4", "plan": TWO_HOP, "weights": {"edge.e1": "nope"}},
    )
    assert response.status_code == 400
    assert response.json()["category"] == "rejected_weights"


def test_invalid_snapshot_shape_rejected(client):
    response = client.post("/v1/snapshots", json={"snapshot": {"version": "v9"}})
    assert response.status_code == 400
    assert response.json()["category"] == "rejected_snapshot"


def test_unknown_run_is_typed_404(client):
    response = client.get("/v1/runs/nope")
    assert response.status_code == 422
    assert response.json()["category"] == "rejected_input_version"
