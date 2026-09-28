"""HTTP interface tests: status codes, error categories, replay, run logs."""

import json
import os

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from tests.conftest import as_pairs


@pytest.fixture()
def client(tmp_path):
    app = create_app(log_dir=str(tmp_path / "logs"))
    with TestClient(app) as c:
        c.log_dir = str(tmp_path / "logs")
        yield c


CUBIC = {"coefficients": as_pairs([1, -6, 11, -6])}  # roots 1, 2, 3


class TestSolveHappyPath:
    def test_solve_returns_sorted_roots_with_evidence(self, client):
        resp = client.post("/v1/solve", json=CUBIC)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "converged"
        assert body["degree"] == 3
        roots = [(r["re"], r["im"]) for r in body["roots"]]
        assert roots == sorted(roots)  # deterministic (re, im) order
        assert [round(r[0]) for r in roots] == [1, 2, 3]
        # Evidence block is populated.
        ev = body["evidence"]
        assert ev["max_residual_rel"] < 1e-12
        assert ev["reconstruction_error"] < 1e-12
        assert ev["vieta_max_deviation"] < 1e-12
        for r in body["roots"]:
            assert r["converged"] is True
            assert r["residual_rel"] < 1e-12

    def test_conjugate_pairing_reported(self, client):
        resp = client.post("/v1/solve",
                           json={"coefficients": as_pairs([2, -3, 7, 7, -5])})
        assert resp.status_code == 200
        pairing = resp.json()["pairing"]
        assert pairing["pair_tol"] == pytest.approx(1e-8)
        assert len(pairing["pairs"]) == 1  # the 1±2i pair
        assert len(pairing["unpaired"]) == 2


class TestErrorCategories:
    def test_zero_polynomial_is_input_invalid(self, client):
        resp = client.post("/v1/solve",
                           json={"coefficients": [[0.0, 0.0], [0.0, 0.0]]})
        assert resp.status_code == 422
        assert resp.json()["error"]["category"] == "input_invalid"

    def test_non_finite_coefficient_is_input_invalid(self, client):
        # NaN is not valid JSON, so send the raw body: the server-side JSON
        # parser accepts the NaN literal and the input boundary must reject it.
        resp = client.post(
            "/v1/solve",
            content='{"coefficients": [[1.0, 0.0], [NaN, 0.0]]}',
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["category"] == "input_invalid"

    def test_degree_cap_is_resource_exhausted(self, client):
        coeffs = [[1.0, 0.0]] + [[0.0, 0.0]] * 30 + [[-1.0, 0.0]]  # degree 31
        resp = client.post("/v1/solve",
                           json={"coefficients": coeffs,
                                 "options": {"max_degree": 10}})
        assert resp.status_code == 413
        assert resp.json()["error"]["category"] == "resource_exhausted"

    def test_state_conflict_on_run_id_reuse(self, client):
        r1 = client.post("/v1/solve", json={**CUBIC, "run_id": "run-x"})
        assert r1.status_code == 200
        # Same run_id, different payload -> conflict.
        r2 = client.post("/v1/solve",
                         json={"coefficients": as_pairs([1, 0, -1]),
                               "run_id": "run-x"})
        assert r2.status_code == 409
        assert r2.json()["error"]["category"] == "state_conflict"

    def test_run_id_replay_is_idempotent(self, client):
        r1 = client.post("/v1/solve", json={**CUBIC, "run_id": "run-y"})
        r2 = client.post("/v1/solve", json={**CUBIC, "run_id": "run-y"})
        assert r1.status_code == r2.status_code == 200
        assert r1.json() == r2.json()
        # And retrievable via the runs endpoint.
        r3 = client.get("/v1/runs/run-y")
        assert r3.status_code == 200
        assert r3.json() == r1.json()

    def test_unknown_run_lookup_404(self, client):
        assert client.get("/v1/runs/nope").status_code == 404


class TestIterationExhaustion:
    def test_partial_status_preserves_unconverged_state(self, client):
        # Pure Aberth from circle guesses with zero budget: nothing can
        # converge; the state must be returned, flagged unconverged.
        resp = client.post("/v1/solve", json={
            **CUBIC,
            "options": {"method": "aberth", "max_iter": 0},
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "partial"
        assert len(body["roots"]) == 3
        assert all(r["converged"] is False for r in body["roots"])
        assert "exhausted" in body["message"]


class TestRunLog:
    def test_log_records_replayable_state_and_reasons(self, client):
        resp = client.post("/v1/solve", json={**CUBIC, "run_id": "log-me"})
        assert resp.status_code == 200
        path = os.path.join(client.log_dir, "runs.jsonl")
        with open(path, encoding="utf-8") as fh:
            events = [json.loads(line) for line in fh]
        mine = [e for e in events if e["run_id"] == "log-me"]
        kinds = [e["event"] for e in mine]
        assert "request" in kinds and "normalized" in kinds and "decision" in kinds
        decision = mine[kinds.index("decision")]
        assert decision["status"] == "converged"
        assert decision["reason"]
        assert "max_residual_rel" in decision  # key intermediate evidence
