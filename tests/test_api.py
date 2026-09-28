"""API layer: problem registration, solving, evidence queries, error mapping."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from csp_service.api import create_app
from csp_service.config import Settings

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture()
def client(tmp_path):
    settings = Settings(db_path=str(tmp_path / "api_test.db"),
                        log_dir=str(tmp_path))
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _fixture(name):
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())["problem"]


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "python" in body["versions"] and "csp_service" in body["versions"]


def test_register_and_fetch_problem(client):
    problem = _fixture("multi_solution")
    assert client.post("/problems", json=problem).status_code == 201
    assert client.get("/problems/multi_solution").json()["name"] == "multi_solution"
    assert client.get("/problems/nope").status_code == 404


def test_solve_inline_and_inspect_evidence(client):
    problem = _fixture("isolated_variable")
    resp = client.post("/solve", json={"problem": problem, "mode": "all"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "SAT"
    assert len(body["solutions"]) == 8
    run_id = body["run_id"]

    run = client.get(f"/runs/{run_id}").json()
    assert run["status"] == "SAT"
    assert run["versions"]["csp_service"]
    assert run["problem"]["name"] == "isolated_variable"

    prunings = client.get(f"/runs/{run_id}/prunings").json()["prunings"]
    # mode=all explores the whole tree; root-node prunings are the AC fixpoint
    root_prunes = [p for p in prunings if p["kind"] == "prune" and p["node"] == 0]
    reasons = {(p["var"], p["value"]): p["reason"] for p in root_prunes}
    assert set(reasons) == {("x", 3), ("y", 1)}
    assert reasons[("x", 3)]["kind"] == "arc_no_support"
    # deeper prunings stay auditable, namespaced by node id
    assert all("node" in p and "depth" in p for p in prunings)

    events = client.get(f"/runs/{run_id}/events", params={"kind": "prune"}).json()
    assert all(e["kind"] == "prune" for e in events["events"])


def test_solve_by_registered_name(client):
    client.post("/problems", json=_fixture("hall_conflict"))
    body = client.post("/solve", json={"problem_name": "hall_conflict"}).json()
    assert body["status"] == "UNSAT"
    assert body["stats"]["nodes"] == 0


def test_budget_exhaustion_is_unknown_not_error(client):
    body = client.post("/solve", json={
        "problem": _fixture("deep_backtrack"), "mode": "all", "max_nodes": 1,
    }).json()
    assert body["status"] == "UNKNOWN"
    assert body["partial"] is True


def test_error_mapping(client):
    assert client.post("/solve", json={}).status_code == 400
    assert client.post("/solve", json={"problem_name": "ghost"}).status_code == 404
    assert client.get("/runs/run-0000-nope").status_code == 404
    bad = {"name": "bad", "variables": [{"name": "x", "domain": [1, 1]}]}
    assert client.post("/problems", json=bad).status_code == 422
