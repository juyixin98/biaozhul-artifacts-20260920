"""End-to-end HTTP tests via FastAPI TestClient.

These assert concrete response values and the distinct failure categories:
input error (400), state conflict (409), resource exhausted (507), and
verification results in the success body.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service.orchestrator import Orchestrator
from app.service.runlog import RunLogger
from app.service.sessions import SessionStore


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    log = RunLogger(tmp_path / "runs.jsonl")
    orch = Orchestrator(SessionStore(), log)
    return TestClient(create_app(orch))


def _create(client: TestClient, fixture: str = "branching", seed: int = 1234):
    r = client.post("/sessions", json={"fixture": fixture, "master_seed": seed})
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


@pytest.mark.e2e
def test_health_and_fixture_catalog(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    names = {f["name"] for f in client.get("/fixtures").json()["fixtures"]}
    assert names == {"linear_chain", "branching", "tight_budget"}


@pytest.mark.e2e
def test_session_reports_shared_nodes_and_shapes(client: TestClient) -> None:
    sid = _create(client)
    info = client.get(f"/sessions/{sid}").json()
    assert "shared" in info["shared_nodes"]
    assert "W3" in info["shared_nodes"]
    assert info["shapes"]["h1"] == [2, 4]
    assert info["planned"] is False


@pytest.mark.e2e
def test_plan_then_run_verifies_against_independent_oracle(client: TestClient
                                                           ) -> None:
    sid = _create(client)
    plan = client.post(f"/sessions/{sid}/plan", json={"rng_strategy": "counter"})
    assert plan.status_code == 200
    body = plan.json()
    assert body["feasible"] is True
    assert body["plan"]["independent_check"]["matches_core"] is True
    # Concrete numbers for the branching fixture under counter strategy.
    assert body["plan"]["recompute_flops"] == 16
    assert body["plan"]["peak_memory"] == 114

    run = client.post(
        f"/sessions/{sid}/runs",
        json={"master_seed": 1234, "finite_difference": True},
    )
    assert run.status_code == 200, run.text
    rj = run.json()
    assert rj["verification"]["passed"] is True
    assert rj["emitted_side_effects"] == 1
    assert rj["rng_replay_ok"] is True
    for pid, diff in rj["verification"]["grad_max_abs_diff"].items():
        assert diff <= 1e-8
    for pid, diff in rj["verification"][
        "finite_difference_max_abs_diff"
    ].items():
        assert diff <= 1e-5
    assert {g["parameter"] for g in rj["gradients"]} == {"W1", "W3"}


@pytest.mark.e2e
def test_snapshot_strategy_run_passes(client: TestClient) -> None:
    sid = _create(client)
    client.post(f"/sessions/{sid}/plan", json={"rng_strategy": "snapshot"})
    r = client.post(f"/sessions/{sid}/runs", json={"master_seed": 1234})
    assert r.status_code == 200
    assert r.json()["verification"]["passed"] is True
    assert r.json()["rng_strategy"] == "snapshot"


@pytest.mark.e2e
def test_infeasible_budget_is_507_resource_exhausted(client: TestClient
                                                     ) -> None:
    sid = _create(client, "tight_budget")
    r = client.post(f"/sessions/{sid}/plan", json={"memory_budget": 1})
    assert r.status_code == 507
    err = r.json()["error"]
    assert err["category"] == "resource_exhausted"
    assert err["code"] == "E_BUDGET_INFEASIBLE"
    assert err["context"]["shortfall"] > 0


@pytest.mark.e2e
def test_run_before_plan_is_409_state_conflict(client: TestClient) -> None:
    sid = _create(client, "linear_chain")
    r = client.post(f"/sessions/{sid}/runs", json={})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "E_STATE_NO_PLAN"


@pytest.mark.e2e
def test_unknown_session_is_400_input_error(client: TestClient) -> None:
    r = client.get("/sessions/nope")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "E_SESSION_UNKNOWN"


@pytest.mark.e2e
def test_bad_rng_strategy_is_422_request_validation(client: TestClient
                                                     ) -> None:
    sid = _create(client)
    r = client.post(f"/sessions/{sid}/plan", json={"rng_strategy": "mersenne"})
    assert r.status_code == 422


@pytest.mark.e2e
def test_double_apply_is_409_and_logged(client: TestClient, tmp_path: Path
                                        ) -> None:
    sid = _create(client, "linear_chain")
    client.post(f"/sessions/{sid}/plan", json={})
    client.post(f"/sessions/{sid}/runs", json={"master_seed": 1234})
    first = client.post(f"/sessions/{sid}/apply", json={"lr": 0.1})
    assert first.status_code == 200
    second = client.post(f"/sessions/{sid}/apply", json={"lr": 0.1})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "E_STATE_RUN_DOUBLE_APPLY"

    log_lines = [
        json.loads(line)
        for line in (tmp_path / "runs.jsonl").read_text().splitlines()
    ]
    applied = [e for e in log_lines if e["event"] == "step_applied"]
    assert len(applied) == 1
    failed = [
        e for e in log_lines
        if e.get("error_code") == "E_STATE_RUN_DOUBLE_APPLY"
    ]
    # The rejected second apply is logged as a state-conflict failure, so
    # the rejected operation is replayable from disk.
    assert len(failed) == 1
    assert failed[0]["error_category"] == "state_conflict"
    assert applied[0]["step_after"] == 1


@pytest.mark.e2e
def test_run_log_contains_replayable_run_id_and_judgement(client: TestClient,
                                                           tmp_path: Path
                                                           ) -> None:
    sid = _create(client)
    client.post(f"/sessions/{sid}/plan", json={})
    r = client.post(f"/sessions/{sid}/runs", json={"master_seed": 1234})
    run_id = r.json()["run_id"]
    records = [
        json.loads(line)
        for line in (tmp_path / "runs.jsonl").read_text().splitlines()
    ]
    completed = [e for e in records if e["event"] == "run_completed"]
    assert len(completed) == 1
    rec = completed[0]
    assert rec["run_id"] == run_id
    assert rec["intermediate"]["simulated_peak"] == rec["intermediate"][
        "peak_memory"
    ]
    assert "replay_waves" in rec["intermediate"]
    assert "grad_fingerprints" in rec["intermediate"]
    assert rec["verification"]["passed"] is True
    assert rec["judgement"].startswith("gradients match")


@pytest.mark.e2e
def test_computation_failure_is_500_and_logged(client: TestClient,
                                               tmp_path: Path,
                                               monkeypatch) -> None:
    import numpy as np

    from app.core import ops

    real_backward = ops.backward

    def nan_backward(op, params, inputs, aux, grad_out, **kwargs):
        grads = real_backward(op, params, inputs, aux, grad_out, **kwargs)
        return [np.full_like(g, np.nan) for g in grads]

    monkeypatch.setattr(ops, "backward", nan_backward)

    sid = _create(client, "linear_chain")
    client.post(f"/sessions/{sid}/plan", json={})
    r = client.post(f"/sessions/{sid}/runs", json={"master_seed": 1234})
    assert r.status_code == 500
    err = r.json()["error"]
    assert err["category"] == "computation_failure"
    assert err["code"] == "E_COMP_NONFINITE"
    records = [
        json.loads(line)
        for line in (tmp_path / "runs.jsonl").read_text().splitlines()
    ]
    failed = [e for e in records if e["event"] == "run_failed"]
    assert failed and failed[-1]["error_category"] == "computation_failure"


@pytest.mark.e2e
def test_custom_graph_session_runs_end_to_end(client: TestClient) -> None:
    payload = {
        "graph": {
            "target": "loss",
            "parameters": {
                "W": {"shape": [2, 2], "seed": 9},
            },
            "inputs": {"x": [[1.0, 0.5], [0.0, -1.0]]},
            "nodes": [
                {"id": "x", "op": "input", "params": {"shape": [2, 2]}},
                {"id": "W", "op": "parameter", "params": {"shape": [2, 2]}},
                {"id": "h", "op": "linear", "inputs": ["x", "W"]},
                {"id": "a", "op": "relu", "inputs": ["h"]},
                {"id": "loss", "op": "reduce_sum", "inputs": ["a"]},
            ],
        }
    }
    r = client.post("/sessions", json=payload)
    assert r.status_code == 201, r.text
    sid = r.json()["session_id"]
    assert client.post(f"/sessions/{sid}/plan", json={}).status_code == 200
    run = client.post(f"/sessions/{sid}/runs", json={"master_seed": 1})
    assert run.status_code == 200
    assert run.json()["verification"]["passed"] is True


@pytest.mark.e2e
def test_custom_graph_with_mul_and_bias_verifies(client: TestClient) -> None:
    # y = sum( relu(x @ W + b) * gate ); covers mul + biased linear.
    payload = {
        "graph": {
            "target": "loss",
            "parameters": {
                "W": {"shape": [2, 3], "seed": 3},
                "b": {"shape": [3], "seed": 4},
            },
            "inputs": {
                "x": [[1.0, -0.5]],
                "gate": [[2.0, 0.0, -1.0]],
            },
            "nodes": [
                {"id": "x", "op": "input", "params": {"shape": [1, 2]}},
                {"id": "gate", "op": "input", "params": {"shape": [1, 3]}},
                {"id": "W", "op": "parameter", "params": {"shape": [2, 3]}},
                {"id": "b", "op": "parameter", "params": {"shape": [3]}},
                {"id": "h", "op": "linear", "inputs": ["x", "W", "b"]},
                {"id": "a", "op": "relu", "inputs": ["h"]},
                {"id": "m", "op": "mul", "inputs": ["a", "gate"]},
                {"id": "loss", "op": "reduce_sum", "inputs": ["m"]},
            ],
        }
    }
    r = client.post("/sessions", json=payload)
    assert r.status_code == 201, r.text
    sid = r.json()["session_id"]
    assert client.post(f"/sessions/{sid}/plan", json={}).status_code == 200
    run = client.post(
        f"/sessions/{sid}/runs",
        json={"master_seed": 1, "finite_difference": True},
    )
    assert run.status_code == 200, run.text
    rj = run.json()
    assert rj["verification"]["passed"] is True
    assert {g["parameter"] for g in rj["gradients"]} == {"W", "b"}
