"""End-to-end HTTP tests via FastAPI TestClient, including log replay checks."""

from __future__ import annotations

import logging

import numpy as np
import pytest
from fastapi.testclient import TestClient

from hvp_service.config import ServiceConfig
from hvp_service.main import create_app
from hvp_service.service import HvpService

from .conftest import quadratic_spec, unary_spec
from .references import QUAD_A, quad_hvp


@pytest.fixture()
def client() -> TestClient:
    service = HvpService(
        ServiceConfig(max_graph_nodes=2_000, max_eval_nodes=50_000, default_time_budget_ms=None)
    )
    return TestClient(create_app(service=service))


def _create_quad(client: TestClient) -> str:
    nodes, output = quadratic_spec()
    resp = client.post("/graphs", json={"nodes": nodes, "output": output})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["layout"]["size"] == 3
    assert body["layout"]["slots"][0]["name"] == "x"
    return body["graph_id"]


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_quadratic_hvp_over_http(client: TestClient) -> None:
    gid = _create_quad(client)
    point = [0.4, -1.1, 2.3]
    direction = [1.0, -0.5, 0.75]
    resp = client.post(f"/graphs/{gid}/hvp", json={"point": point, "vector": direction})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    np.testing.assert_allclose(body["hvp"], quad_hvp(np.array(direction)), atol=1e-12)
    np.testing.assert_allclose(body["gradient"], QUAD_A @ np.array(point) + np.array([0.3, -0.7, 1.2]), atol=1e-12)
    assert body["run_id"]
    assert body["diagnostics"]["run_id"] == body["run_id"]
    assert body["layout"]["size"] == 3


def test_error_payload_for_unknown_graph(client: TestClient) -> None:
    resp = client.post("/graphs/nope/hvp", json={"point": [1.0], "vector": [1.0]})
    assert resp.status_code == 404
    err = resp.json()["error"]
    assert err["category"] == "not_found"
    assert "message" in err


def test_error_payload_for_bad_vector_length(client: TestClient) -> None:
    gid = _create_quad(client)
    resp = client.post(f"/graphs/{gid}/hvp", json={"point": [1.0, 2.0, 3.0], "vector": [1.0]})
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["category"] == "input_validation"
    assert err["details"]["expected_size"] == 3


def test_error_payload_for_nonsmooth_reject(client: TestClient) -> None:
    nodes, output = unary_spec("abs")
    resp = client.post("/graphs", json={"nodes": nodes, "output": output})
    gid = resp.json()["graph_id"]
    resp = client.post(f"/graphs/{gid}/hvp", json={"point": [0.0, 2.0], "vector": [1.0, 1.0]})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["category"] == "nonsmooth_point"
    assert err["run_id"]
    assert err["details"]["op"] == "abs"


def test_error_payload_for_schema_violation(client: TestClient) -> None:
    gid = _create_quad(client)
    resp = client.post(f"/graphs/{gid}/hvp", json={"point": [1.0, 2.0, 3.0]})  # missing vector
    assert resp.status_code == 400
    assert resp.json()["error"]["category"] == "input_validation"


def test_stored_point_flow_over_http(client: TestClient) -> None:
    gid = _create_quad(client)
    resp = client.post(f"/graphs/{gid}/point", json={"point": [0.4, -1.1, 2.3]})
    assert resp.status_code == 200
    assert resp.json()["point_version"] == 1
    resp = client.post(
        f"/graphs/{gid}/hvp",
        json={"vector": [1.0, -0.5, 0.75], "use_stored_point": True, "expected_version": 1},
    )
    assert resp.status_code == 200
    resp = client.post(
        f"/graphs/{gid}/hvp",
        json={"vector": [1.0, -0.5, 0.75], "use_stored_point": True, "expected_version": 99},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["category"] == "state_conflict"


def test_run_id_appears_in_structured_logs(client: TestClient, caplog) -> None:
    gid = _create_quad(client)
    with caplog.at_level(logging.INFO, logger="hvp_service"):
        resp = client.post(
            f"/graphs/{gid}/hvp",
            json={"point": [0.4, -1.1, 2.3], "vector": [1.0, -0.5, 0.75]},
        )
    run_id = resp.json()["run_id"]
    events = [
        getattr(record, "fields", {})
        for record in caplog.records
        if record.name == "hvp_service"
    ]
    start = [f for f in events if f.get("event") == "hvp_start" and f.get("run_id") == run_id]
    done = [f for f in events if f.get("event") == "hvp_done" and f.get("run_id") == run_id]
    assert start, "hvp_start log with run_id must exist for replay"
    assert done, "hvp_done log with run_id must exist for replay"
    assert done[0]["nodes_evaluated"] > 0
    assert "elapsed_ms" in done[0]
