"""End-to-end HTTP tests for the FastAPI query interface.

Uses an isolated SQLite file per test and asserts concrete planner results,
request correlation, failure separation, and identity headers -- not merely
that the endpoints respond.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from htn_planner.api import create_app
from htn_planner.config import Settings

pytestmark = pytest.mark.e2e
from htn_planner.service import PlanningService
from htn_planner.storage import EvidenceStore

from .fixture_loader import PROBLEM_DIR


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    fixture_root = PROBLEM_DIR.parent
    settings = Settings(
        db_path=str(tmp_path / "api.db"),
        fixture_dir=str(fixture_root),
        domain_dir=str(fixture_root / "domains"),
        log_level="INFO",
        service_name="finite-htn-planner",
        service_version="1.0.0",
    )
    store = EvidenceStore(settings.db_path)
    service = PlanningService(settings=settings, store=store)
    return TestClient(create_app(service=service, store=store))


def test_health_reports_identity_and_domains(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "finite-htn-planner"
    assert body["version"] == "1.0.0"
    assert set(body["domains"]) == {"assembly", "logistics"}
    assert resp.headers["x-service-version"] == "1.0.0"


def test_fixture_plan_returns_feasible_result_and_correlates_id(
    client: TestClient,
) -> None:
    resp = client.post(
        "/plan/fixture",
        json={"problem": "logistics_via_hub", "request_id": "req-http-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["feasible"] is True
    assert body["request_id"] == "req-http-1"
    assert body["domain"] == "logistics" and body["domain_version"] == "1.0.0"
    # Concrete recursive result: two haul legs, eight leaf actions.
    assert len(body["execution_order"]) == 8

    fetched = client.get("/plans/req-http-1").json()
    assert fetched["request_id"] == "req-http-1"
    assert fetched["execution_order"] == body["execution_order"]


def test_failure_endpoint_separates_terminal_from_abandoned(client: TestClient) -> None:
    client.post(
        "/plan/fixture",
        json={"problem": "logistics_partial_conflict", "request_id": "req-http-f"},
    )
    evidence = client.get("/plans/req-http-f/failures").json()["evidence"]
    kinds = [e["kind"] for e in evidence]
    assert "partial_order_infeasible" in kinds
    # A feasible plan's abandoned branches live under a different run.
    client.post(
        "/plan/fixture",
        json={"problem": "logistics_via_hub", "request_id": "req-http-ok"},
    )
    ev2 = client.get("/plans/req-http-ok/failures").json()["evidence"]
    assert ev2 and all(e["branch"] == "abandoned" for e in ev2)


def test_tree_endpoint_returns_expansion_hierarchy(client: TestClient) -> None:
    client.post(
        "/plan/fixture",
        json={"problem": "assembly_deep", "request_id": "req-http-tree"},
    )
    tree = client.get("/plans/req-http-tree/tree").json()
    compounds = [n for n in tree["nodes"] if n["kind"] == "compound"]
    leaves = [n for n in tree["nodes"] if n["kind"] == "primitive"]
    assert len(compounds) == 5 and len(leaves) == 5
    root = next(n for n in tree["nodes"] if n["node_id"] == tree["roots"][0])
    assert root["method"] == "m-assemble-composite"


def test_audit_endpoint_returns_ordered_key_steps(client: TestClient) -> None:
    client.post(
        "/plan/fixture",
        json={"problem": "logistics_direct", "request_id": "req-http-audit"},
    )
    audit = client.get("/plans/req-http-audit/audit").json()["audit"]
    locations = [a["location"] for a in audit]
    assert locations[0] == "service:receive"
    assert locations[-1] == "service:respond"
    assert any(loc.startswith("kernel:method-select") for loc in locations)
    assert all(a["message"].count("req-http-audit") >= 1
               or loc.startswith("kernel:") for a, loc in zip(audit, locations))


def test_inline_plan_validates_and_rejects_bad_domain(client: TestClient) -> None:
    resp = client.post(
        "/plan/inline",
        json={
            "problem": {
                "name": "x", "domain": "ghost",
                "goal_task": "g",
            },
            "request_id": "req-inline-bad",
        },
    )
    assert resp.status_code == 404
    assert "unknown domain" in resp.json()["detail"]


def test_inline_plan_accepts_well_formed_body(client: TestClient) -> None:
    resp = client.post(
        "/plan/inline",
        json={
            "problem": {
                "name": "tok", "domain": "logistics",
                "initial_facts": [["token_ready"]],
                "goal_task": "two_consume", "goal_args": ["a", "b"],
            }
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["feasible"] is False
    kinds = [f["kind"] for f in body["failures"]]
    assert "partial_order_infeasible" in kinds
    assert body["request_id"].startswith("req-")


def test_missing_run_returns_404(client: TestClient) -> None:
    assert client.get("/plans/nope").status_code == 404
    assert client.get("/plans/nope/tree").status_code == 404
    assert client.get("/plans/nope/audit").status_code == 404


def test_plans_index_lists_runs(client: TestClient) -> None:
    client.post("/plan/fixture", json={"problem": "logistics_direct"})
    listing = client.get("/plans").json()["plans"]
    assert len(listing) == 1
    assert listing[0]["feasible"] in (0, 1)
