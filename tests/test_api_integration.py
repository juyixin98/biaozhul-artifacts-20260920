"""End-to-end HTTP integration tests against the FastAPI app.

Uses an isolated temp SQLite database per test (no network listeners).
Assertions cover concrete labels, nogood filtering, retraction results,
diagnostic fields and failure categories -- not just status codes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atms_backend.api.app import create_app
from atms_backend.core.budgets import Budgets


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "it.db"))
    # Lifespan (TestClient context manager) opens the SQLite store.
    with TestClient(app) as c:
        yield c


SHARED = """
assume A, B, C
fact P
rule r1: A, P => X
rule r2: B, P => X
rule r3: X => Y
rule r4: C => Z
rule r5: A, C => FALSE
"""


def _create(client, pid="p1", source=SHARED):
    resp = client.post(
        "/problems",
        json={"id": pid, "name": "shared reasoning", "source": source},
    )
    assert resp.status_code == 201, resp.text
    return resp


def test_health_and_create_and_list(client):
    assert client.get("/health").json() == {"status": "ok"}
    body = _create(client).json()
    assert body["request_id"].startswith("req-")
    assert body["nodes"] == ["A", "B", "C", "P", "X", "Y", "Z"]
    listing = client.get("/problems").json()["problems"]
    assert [p["id"] for p in listing] == ["p1"]


def test_propagate_returns_hand_computed_labels_and_nogood(client):
    _create(client)
    resp = client.post("/problems/p1/propagate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["incomplete"] is False
    assert body["nogoods"] == [["A", "C"]]
    assert body["labels"]["X"] == [["A"], ["B"]]
    assert body["labels"]["Y"] == [["A"], ["B"]]
    assert body["labels"]["Z"] == [["C"]]
    assert body["labels"]["P"] == [[]]


def test_query_accept_reject_with_request_id_and_state(client):
    _create(client)
    # Consistent context -> accepted.
    r1 = client.post(
        "/problems/p1/query",
        json={"node_id": "X", "environment": ["B", "C"]},
        headers={"X-Request-ID": "fixed-id-1"},
    ).json()
    assert r1["decision"] == "accepted"
    assert r1["supporting_environments"] == [["B"]]
    assert r1["request_id"] == "fixed-id-1"

    # Context containing the nogood -> rejected, blocker named.
    r2 = client.post(
        "/problems/p1/query", json={"node_id": "X", "environment": ["A", "C"]}
    ).json()
    assert r2["decision"] == "rejected"
    assert r2["reason"] == "blocked_by_nogood"
    assert r2["nogood_blockers"] == [["A", "C"]]

    # Unknown node in complete theory -> no-env rejection.
    r3 = client.post("/problems/p1/query", json={"node_id": "GHOST"}).json()
    assert r3["decision"] == "rejected"
    assert r3["reason"] == "no_consistent_environment"


def test_retraction_endpoint_keeps_alternatively_supported(client):
    _create(client)
    resp = client.post("/problems/p1/retract", json={"assumptions": ["A"]})
    assert resp.status_code == 200
    body = resp.json()
    assert "X" in body["surviving_nodes"]
    assert body["nogoods"] == []
    x_change = next(c for c in body["changed"] if c["node_id"] == "X")
    assert x_change["after"] == [["B"]]

    # Retraction is hypothetical: a fresh query still sees both proofs.
    again = client.post("/problems/p1/query", json={"node_id": "X"}).json()
    assert again["supporting_environments"] == [["A"], ["B"]]


def test_labels_and_nogoods_endpoints(client):
    _create(client)
    labels = client.get("/problems/p1/labels").json()
    assert labels["nogoods"] == [["A", "C"]]
    nogoods = client.get("/problems/p1/nogoods").json()
    assert nogoods["nogoods"] == [["A", "C"]]


def test_explain_endpoint_lists_rules_per_environment(client):
    _create(client)
    body = client.get("/problems/p1/nodes/X/explain").json()
    by_env = {tuple(e["environment"]): e["rules"] for e in body["explanations"]}
    assert by_env[("A",)] == ["r1"]
    assert by_env[("B",)] == ["r2"]


def test_propagation_runs_are_recorded_with_request_ids(client):
    _create(client)
    client.post(
        "/problems/p1/propagate", headers={"X-Request-ID": "audit-1"}
    )
    runs = client.get("/problems/p1/runs").json()["runs"]
    assert any(r["request_id"] == "audit-1" and r["incomplete"] == 0 for r in runs)


def test_budget_override_reports_incomplete(client):
    client.post(
        "/problems",
        json={
            "id": "bud",
            "name": "budget",
            "source": (
                "assume A1, A2, A3\n"
                "rule r1: A1 => X\nrule r2: A2 => X\nrule r3: A3 => LATE\n"
            ),
        },
    )
    body = client.post(
        "/problems/bud/propagate",
        json={"max_label_envs": 1, "max_total_envs": 1000, "max_steps": 1000},
    ).json()
    assert body["incomplete"] is True
    assert "label_envs" in body["reason"]

    q = client.post("/problems/bud/query", json={"node_id": "LATE"}).json()
    # Default-budget query completes and finds LATE (incomplete runs are not
    # persisted as truth).
    assert q["decision"] == "accepted"
    assert q["supporting_environments"] == [["A3"]]


def test_error_categories_are_explicit(client):
    # Unknown problem -> 404.
    assert client.get("/problems/nope/labels").status_code == 404
    # Duplicate create -> 409.
    _create(client)
    dup = client.post(
        "/problems", json={"id": "p1", "name": "x", "source": SHARED}
    )
    assert dup.status_code == 409
    # Bad DSL -> 422 with line number and request id.
    bad = client.post(
        "/problems",
        json={"id": "bad", "name": "bad", "source": "assume A\nrule r: A, Q => X\n"},
    )
    assert bad.status_code == 422
    detail = bad.json()["detail"]
    assert detail["error"] == "rule_language_error"
    assert detail["line"] == 2
    assert detail["request_id"].startswith("req-")
    # Retracting an undeclared assumption -> 400 category.
    err = client.post("/problems/p1/retract", json={"assumptions": ["NOPE"]})
    assert err.status_code == 400
    # Querying inside a context naming an undeclared assumption -> 400.
    bad_ctx = client.post(
        "/problems/p1/query", json={"node_id": "X", "environment": ["NOPE"]}
    )
    assert bad_ctx.status_code == 400
    assert "NOPE" in bad_ctx.json()["detail"]
