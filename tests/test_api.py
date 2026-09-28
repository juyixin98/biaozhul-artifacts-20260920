"""Integration tests for the FastAPI query interface.

Uses an in-process ASGI client and an in-memory/temp SQLite database; no
network sockets or external services are needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from htn_planner.api import create_app
from htn_planner.config import Settings
from htn_planner.core.engine import Bounds

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
DOMAINS = FIXTURES / "domains"
PROBLEMS = FIXTURES / "problems"


@pytest.fixture
def client(tmp_path) -> TestClient:
    settings = Settings(db_path=str(tmp_path / "api.db"), bounds=Bounds(max_depth=12))
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _body(problem_file: str = "logistics_direct.pddl", domain_file: str = "logistics.htn", **extra):
    payload = {
        "domain": (DOMAINS / domain_file).read_text(),
        "problem": (PROBLEMS / problem_file).read_text(),
        "domain_version": "test-1",
    }
    payload.update(extra)
    return payload


class TestHealthAndIdentity:
    def test_health(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_request_id_is_echoed_and_generated(self, client: TestClient) -> None:
        resp = client.post("/api/v1/plan", json=_body(), headers={"X-Request-ID": "abc-123"})
        assert resp.status_code == 200
        assert resp.headers["x-request-id"] == "abc-123"
        assert resp.json()["request_id"] == "abc-123"

    def test_request_id_auto_generated_when_absent(self, client: TestClient) -> None:
        resp = client.post("/api/v1/plan", json=_body())
        assert resp.status_code == 200
        assert resp.headers["x-request-id"].startswith("req-")


class TestPlanEndpoint:
    def test_successful_plan_shape(self, client: TestClient) -> None:
        resp = client.post("/api/v1/plan", json=_body(), headers={"X-Request-ID": "r1"})
        data = resp.json()
        result = data["result"]
        assert result["status"] == "success"
        assert result["meta"]["engine_version"]
        assert result["meta"]["domain_version"] == "test-1"
        operators = [a["operator"] for a in result["plan"]]
        assert operators == ["!load", "!drive", "!unload"]
        # Expansion tree present from abstract root down to primitives.
        kinds = {n["kind"] for n in result["expansion_tree"]}
        assert {"network", "compound", "primitive"} <= kinds
        # Failures and uncertainty are separate (empty here).
        assert result["failures"] == []
        assert result["uncertain"] == []
        # Key steps narrate the run and are request-correlated.
        names = [s["name"] for s in result["key_steps"]]
        assert "root_network" in names and "method_applied" in names
        assert "solution" in names
        # Independent verification is embedded.
        verification = data["verification"]
        assert verification["ok"] is True
        assert verification["executable"] is True

    def test_failure_response_is_explanatory(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/plan",
            json=_body(problem_file="logistics_unreachable.pddl"),
            headers={"X-Request-ID": "r-fail"},
        )
        assert resp.status_code == 200
        result = resp.json()["result"]
        assert result["status"] == "failure"
        failure = result["failures"][0]
        assert failure["category"] == "no_applicable_method"
        assert {m["method"] for m in failure["rejected_methods"]} == {
            "m-direct",
            "m-via-hub",
        }
        assert resp.json()["verification"]["failure_sound"] is True

    def test_deadlock_feasible_linearizations_zero(self, client: TestClient) -> None:
        body = _body(
            problem_file="permit_two.pddl", domain_file="permit_deadlock.htn"
        )
        resp = client.post("/api/v1/plan", json=body, headers={"X-Request-ID": "r-dl"})
        result = resp.json()["result"]
        assert result["status"] == "failure"
        assert result["failures"][0]["category"] == "deadlock"

    def test_inconclusive_is_a_distinct_status(self, client: TestClient) -> None:
        body = _body(
            problem_file="bounds_walk_long.pddl",
            domain_file="bounds.htn",
            bounds={"max_depth": 1},
        )
        resp = client.post("/api/v1/plan", json=body, headers={"X-Request-ID": "r-inc"})
        result = resp.json()["result"]
        assert result["status"] == "inconclusive"
        assert result["failures"] == []
        assert {u["category"] for u in result["uncertain"]} == {"depth_budget"}
        # Verifier must not claim correctness for a budget-truncated search.
        assert resp.json()["verification"]["ok"] is False

    def test_parse_error_is_422_with_request_id(self, client: TestClient) -> None:
        payload = {"domain": "(:domain broken", "problem": "(:problem p)"}
        resp = client.post("/api/v1/plan", json=payload, headers={"X-Request-ID": "pe"})
        assert resp.status_code == 422
        assert resp.json()["request_id"] == "pe"
        assert "rule language" in resp.json()["detail"]

    def test_invalid_bound_key_rejected(self, client: TestClient) -> None:
        body = _body(bounds={"no_such_bound": 3})
        resp = client.post("/api/v1/plan", json=body)
        assert resp.status_code == 422
        assert "bound" in resp.json()["detail"].lower()


class TestRunRetrieval:
    def test_plan_then_get_run_and_verification(self, client: TestClient) -> None:
        post = client.post("/api/v1/plan", json=_body(), headers={"X-Request-ID": "persist-1"})
        assert post.status_code == 200

        run = client.get("/api/v1/runs/persist-1")
        assert run.status_code == 200
        assert run.json()["run"]["request_id"] == "persist-1"

        ver = client.get("/api/v1/runs/persist-1/verification")
        assert ver.status_code == 200
        assert ver.json()["verification"]["ok"] == 1

    def test_get_missing_run_is_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/runs/nope").status_code == 404
        assert client.get("/api/v1/runs/nope/verification").status_code == 404

    def test_list_runs_carries_request_identity(self, client: TestClient) -> None:
        client.post("/api/v1/plan", json=_body(), headers={"X-Request-ID": "list-1"})
        resp = client.get("/api/v1/runs", headers={"X-Request-ID": "list-view"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["request_id"] == "list-view"
        assert body["count"] >= 1
        assert any(r["request_id"] == "list-1" for r in body["runs"])

    def test_duplicate_request_id_conflicts(self, client: TestClient) -> None:
        client.post("/api/v1/plan", json=_body(), headers={"X-Request-ID": "dup"})
        second = client.post("/api/v1/plan", json=_body(), headers={"X-Request-ID": "dup"})
        assert second.status_code == 409
