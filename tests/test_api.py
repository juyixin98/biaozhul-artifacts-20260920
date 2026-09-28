"""End-to-end HTTP tests against the FastAPI app using local synthetic data."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from tensor_mem import fixtures as fx  # noqa: E402
from tensor_mem.api import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    app = create_app(log_dir=str(tmp_path / "logs"))
    with TestClient(app) as c:
        yield c


def _diamond_spec(budget=None):
    return {
        "feeds": [{"name": "x", "dtype": "float32", "shape": [4, 4]}],
        "nodes": [
            {"id": "n_relu", "op": "relu", "inputs": ["x"], "outputs": ["t"]},
            {"id": "n_add_branch", "op": "add", "inputs": ["t", "x"], "outputs": ["a"]},
            {"id": "n_mul_branch", "op": "mul", "inputs": ["t", "t"], "outputs": ["m"]},
            {"id": "n_join", "op": "add", "inputs": ["a", "m"], "outputs": ["y"]},
        ],
        "outputs": ["y"],
        "budget": budget,
    }


@pytest.mark.e2e
def test_health_and_ops_catalog(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    ops = client.get("/ops").json()
    assert {"relu", "add", "matmul", "reshape", "transpose"} <= set(ops)


@pytest.mark.e2e
def test_create_session_reports_plan_and_waves(client):
    r = client.post("/sessions", json=_diamond_spec())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["waves"] == [
        ["n_relu"], ["n_add_branch", "n_mul_branch"], ["n_join"]
    ]
    assert body["plan"]["peak_resident_bytes"] == 256
    assert body["plan"]["slots"] < body["plan"]["records"]


@pytest.mark.e2e
def test_bad_graph_returns_graph_validation_category(client):
    spec = _diamond_spec()
    spec["nodes"][1]["inputs"] = ["t", "ghost"]
    r = client.post("/sessions", json=spec)
    assert r.status_code == 400
    body = r.json()
    assert body["category"] == "graph_validation_error"
    assert body["details"]["input"] == "ghost"


@pytest.mark.e2e
def test_budget_exceeded_at_session_creation_is_507(client):
    r = client.post("/sessions", json=_diamond_spec(budget=100))
    assert r.status_code == 507
    assert r.json()["category"] == "resource_exhausted"


@pytest.mark.e2e
def test_execute_and_compare_with_independent_oracle(client):
    case = fx.diamond_case(seed=11)
    sid = client.post("/sessions", json=_diamond_spec()).json()["session_id"]
    r = client.post(
        f"/sessions/{sid}/execute",
        json={"feeds": {"x": {"data": case.feeds["x"].tolist()}}, "run_id": "http-1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"] == "http-1"
    got = np.asarray(body["outputs"]["y"]["data"], dtype=np.float32)
    np.testing.assert_allclose(got, fx.reference_outputs(case)["y"], rtol=1e-6)
    assert body["output_shapes"] == {"y": [4, 4]}

    rel = client.post(f"/sessions/{sid}/release", json={"run_id": "http-1"})
    assert rel.status_code == 200
    assert rel.json()["retained_bytes"] == 0


@pytest.mark.e2e
def test_execute_unknown_feed_is_422_input_error(client):
    sid = client.post("/sessions", json=_diamond_spec()).json()["session_id"]
    r = client.post(
        f"/sessions/{sid}/execute",
        json={"feeds": {"nope": {"data": [[1.0]]}}},
    )
    assert r.status_code == 422
    assert r.json()["category"] == "input_error"


@pytest.mark.e2e
def test_unknown_session_is_422(client):
    r = client.post("/sessions/nope/execute", json={"feeds": {}})
    assert r.status_code == 422
    assert r.json()["category"] == "input_error"


@pytest.mark.e2e
def test_double_release_is_409_state_conflict(client):
    sid = client.post("/sessions", json=_diamond_spec()).json()["session_id"]
    case = fx.diamond_case()
    client.post(
        f"/sessions/{sid}/execute",
        json={"feeds": {"x": {"data": case.feeds["x"].tolist()}}, "run_id": "r1"},
    )
    first = client.post(f"/sessions/{sid}/release", json={"run_id": "r1"})
    assert first.status_code == 200
    second = client.post(f"/sessions/{sid}/release", json={"run_id": "r1"})
    assert second.status_code == 409
    assert second.json()["category"] == "state_conflict"


@pytest.mark.e2e
def test_synthetic_diamond_endpoint_matches_oracle(client):
    r = client.post("/synthetic", json={"case": "diamond", "seed": 11})
    assert r.status_code == 201, r.text
    body = r.json()
    case = fx.diamond_case(seed=11)
    got = np.asarray(body["outputs"]["y"]["data"], dtype=np.float32)
    np.testing.assert_allclose(got, fx.reference_outputs(case)["y"], rtol=1e-6)


@pytest.mark.e2e
def test_synthetic_dynamic_shape_jump_reports_replan(client):
    r1 = client.post(
        "/synthetic", json={"case": "dynamic_matmul", "seed": 3,
                             "params": {"m": 4, "k": 4, "n": 2}}
    )
    sid = r1.json()["session_id"]
    assert r1.json()["replanned"] is False
    # second run on the SAME session with a much bigger batch
    case = fx.dynamic_case(m=32, k=4, n=16, seed=3)
    r2 = client.post(
        f"/sessions/{sid}/execute",
        json={
            "feeds": {
                "a": {"data": case.feeds["a"].tolist()},
                "b": {"data": case.feeds["b"].tolist()},
            },
            "run_id": "grow",
        },
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["replanned"] is True
    assert body["capacity_deficits"]
    got = np.asarray(body["outputs"]["y"]["data"], dtype=np.float32)
    np.testing.assert_allclose(got, fx.reference_outputs(case)["y"], rtol=1e-5)
    assert body["output_shapes"] == {"y": [32, 16]}


@pytest.mark.e2e
def test_synthetic_unknown_case_is_input_error(client):
    r = client.post("/synthetic", json={"case": "nonsense"})
    assert r.status_code == 422
    assert r.json()["category"] == "input_error"


@pytest.mark.e2e
def test_retained_output_budget_failure_over_http_is_507(client):
    sid = client.post(
        "/sessions", json=_diamond_spec(budget=256)
    ).json()["session_id"]
    case = fx.diamond_case()
    payload = {"feeds": {"x": {"data": case.feeds["x"].tolist()}}}
    first = client.post(f"/sessions/{sid}/execute", json={**payload, "run_id": "a"})
    assert first.status_code == 200
    second = client.post(f"/sessions/{sid}/execute", json={**payload, "run_id": "b"})
    assert second.status_code == 507
    body = second.json()
    assert body["category"] == "resource_exhausted"
    assert body["details"]["charged_peak_bytes"] == 320
