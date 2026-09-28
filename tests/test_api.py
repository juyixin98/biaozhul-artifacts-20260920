"""HTTP boundary tests: envelope contract and category->status mapping."""
from __future__ import annotations

import numpy as np
import pytest


from fastapi.testclient import TestClient

from tenmem.api.app import create_app
from tenmem.fixtures import build_diamond, diamond_feeds

pytestmark = pytest.mark.integration


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "logs"))
    return TestClient(app)


def test_health_and_cases(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "ok"
    names = client.get("/cases").json()["data"]["cases"]
    assert set(names) == {
        "diamond", "long_lived", "shape_mutate",
        "parallel_branches", "alias_chain", "workspace_matmul",
    }


def test_run_diamond_case_returns_equivalent_outputs(client) -> None:
    r = client.post("/runs", json={"case": "diamond", "case_params": {"n": 8}, "run_id": "api-d"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    data = body["data"]
    assert data["numerically_equivalent"] is True
    assert data["plan"]["peak_bytes"] < data["no_reuse_peak_bytes"]
    x = diamond_feeds(8)["x"]
    expected = np.maximum(x, 0.0)
    expected = (expected + x) + (np.maximum(x, 0.0) * x)
    np.testing.assert_allclose(data["outputs"]["y"], expected, rtol=1e-6)


def test_parallel_case_runs_threaded(client) -> None:
    r = client.post(
        "/runs",
        json={"case": "parallel_branches", "parallel": True, "run_id": "api-p"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["numerically_equivalent"] is True


def test_shape_jump_is_served_via_replan(client) -> None:
    payload = {
        "case": "shape_mutate",
        "case_params": {"capacity": 64, "bound_n": 32, "n": 48},
        "run_id": "api-replan",
    }
    r = client.post("/runs", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["replanned"] is True
    assert data["replan_reason"]["category"] == "replanning_required"
    assert len(data["outputs"]["act"]) == 48
    assert data["numerically_equivalent"] is True


def test_resource_exhausted_maps_to_507(client) -> None:
    r = client.post(
        "/runs",
        json={"case": "diamond", "case_params": {"n": 8}, "max_bytes": 1},
    )
    assert r.status_code == 507
    err = r.json()["error"]
    assert err["category"] == "resource_exhausted"
    assert err["details"]["max_bytes"] == 1


def test_bad_case_name_is_input_error_422(client) -> None:
    r = client.post("/runs", json={"case": "nope"})
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "input_error"


def test_malformed_graph_is_input_error(client) -> None:
    r = client.post("/plan", json={"graph": {"name": "x"}, "alignment": 64})
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "input_error"


def test_release_unknown_run_is_state_conflict_409(client) -> None:
    r = client.post("/runs/ghost/release", json={"output": "y"})
    assert r.status_code == 409
    assert r.json()["error"]["category"] == "state_conflict"


def test_trace_endpoint_replays_run(client) -> None:
    client.post("/runs", json={"case": "diamond", "run_id": "api-trace"})
    r = client.get("/runs/api-trace/trace")
    assert r.status_code == 200
    kinds = [rec["kind"] for rec in r.json()["data"]["records"]]
    assert "run_start" in kinds and "verdict" in kinds
    assert client.get("/runs/missing/trace").status_code == 404


def test_plan_endpoint_reports_buffers_and_placements(client) -> None:
    graph_dict = build_diamond(8).to_dict()
    r = client.post("/plan", json={"graph": graph_dict})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["conflicts_checked"] is True
    assert data["peak_bytes"] < data["no_reuse_bytes"]
    assert all(set(buf) >= {"id", "capacity", "owners"} for buf in data["buffers"])


def test_case_and_graph_together_rejected(client) -> None:
    r = client.post(
        "/runs",
        json={"case": "diamond", "graph": build_diamond(8).to_dict()},
    )
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "input_error"


def test_neither_case_nor_graph_rejected(client) -> None:
    r = client.post("/runs", json={})
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "input_error"


def test_custom_graph_submission_with_numeric_feeds(client) -> None:
    graph = {
        "name": "custom",
        "inputs": [{"name": "x", "shape": [4], "dtype": "float32"}],
        "constants": [],
        "nodes": [
            {"id": "r", "op": "relu", "inputs": ["x"],
             "outputs": [{"name": "r", "shape": [4], "dtype": "float32"}], "config": {}, "aliases": []},
            {"id": "d", "op": "add", "inputs": ["r", "x"],
             "outputs": [{"name": "y", "shape": [4], "dtype": "float32"}], "config": {}, "aliases": []},
        ],
        "outputs": ["y"],
    }
    body = {"graph": graph, "feeds": {"x": [-1.0, 2.0, -3.0, 4.0]}, "run_id": "api-custom"}
    r = client.post("/runs", json=body)
    assert r.status_code == 200, r.text
    np.testing.assert_allclose(
        r.json()["data"]["outputs"]["y"],
        np.array([-1.0, 4.0, -3.0, 8.0], dtype=np.float32),
    )


def test_unknown_feed_name_in_custom_graph_is_422(client) -> None:
    graph = {
        "name": "custom",
        "inputs": [{"name": "x", "shape": [4], "dtype": "float32"}],
        "nodes": [
            {"id": "r", "op": "relu", "inputs": ["x"],
             "outputs": [{"name": "r", "shape": [4], "dtype": "float32"}]},
        ],
        "outputs": ["r"],
    }
    r = client.post("/runs", json={"graph": graph, "feeds": {"bogus": [1, 2, 3, 4]}})
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "input_error"
