"""Tests 7-8: determinism of disturbances/sim, residual verification, API."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from mpc.api import app
from mpc.config import MPCConfig
from mpc.disturbance import DisturbanceSpec, apply_disturbance
from mpc.mpc import FallbackReason, MPCController, RESIDUAL_TOL
from mpc.simulation import constant_reference, rollout, step_reference


def test_disturbance_is_deterministic_and_bounded_in_form():
    s = DisturbanceSpec(kind="sine", amplitude=0.3, frequency=1.0)
    seq1 = [apply_disturbance(s, k, 0.1) for k in range(50)]
    seq2 = [apply_disturbance(s, k, 0.1) for k in range(50)]
    assert seq1 == seq2
    assert all(abs(v[1]) <= 0.3 + 1e-12 for v in seq1)
    bump = DisturbanceSpec(kind="bump", amplitude=0.5, step=7)
    vals = [apply_disturbance(bump, k, 0.1)[1] for k in range(10)]
    assert vals[7] == 0.5 and vals[6] == 0.0 and vals[8] == 0.0
    with pytest.raises(ValueError):
        apply_disturbance(DisturbanceSpec(kind="nope"), 0, 0.1)


def test_rollout_reproducible(monkeypatch=None):
    cfg = MPCConfig()
    refs = step_reference((0, 0), (1, 0), 15, 40, cfg.horizon)
    spec = DisturbanceSpec(kind="square", amplitude=0.08, frequency=0.3)
    out1 = rollout(MPCController(cfg), [0.1, -0.2], refs, 40, spec)
    out2 = rollout(MPCController(cfg), [0.1, -0.2], refs, 40, spec)
    f1 = [s.state for s in out1.steps]
    f2 = [s.state for s in out2.steps]
    assert f1 == f2
    assert out1.max_acceleration_violation <= 1e-6
    assert out1.max_velocity_violation <= 1e-6


def test_constraint_residual_failure_is_caught():
    """A 'solution' violating constraints must be rejected, not applied."""
    ctrl = MPCController()
    ref = constant_reference((1.0, 0.0), ctrl.config.horizon + 1)
    qp = ctrl.builder.build(np.array([0.0, 0.0]), ref, 0.0)

    bad_z = np.full(ctrl.config.horizon, ctrl.config.a_max + 5.0)
    ok, res = ctrl._verify_solution(qp, bad_z, np.array([0.0, 0.0]), 0.0)
    assert ok is False
    assert res["a_max_violation"] > RESIDUAL_TOL


def test_qp_matrices_change_with_state_but_share_structure():
    cfg = MPCConfig()
    ctrl = MPCController(cfg)
    ref = constant_reference((1.0, 0.0), cfg.horizon + 1)
    q1 = ctrl.builder.build(np.array([0.0, 0.0]), ref, 0.0)
    q2 = ctrl.builder.build(np.array([0.5, -0.3]), ref, 0.2)
    # linear vectors shift; P (Hessian) is state-independent
    assert np.allclose(q1.P.toarray(), q2.P.toarray())
    assert not np.allclose(q1.q, q2.q)
    assert not np.allclose(q1.lower, q2.lower)


# ----------------------------------------------------------------------- #
# HTTP API
# ----------------------------------------------------------------------- #
client = TestClient(app)


def _ref(p=1.0, v=0.0):
    return [[p, v] for _ in range(21)]


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_solve_endpoint_ok():
    r = client.post("/api/mpc/solve",
                    json={"state": [0.0, 0.0], "reference": _ref()})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert abs(body["control"]) <= 3.0 + 1e-9
    assert len(body["predicted_states"]) == 21
    assert body["residuals"]["v_max_violation"] <= 1e-5


def test_solve_endpoint_bad_reference_shape():
    r = client.post("/api/mpc/solve",
                    json={"state": [0.0, 0.0], "reference": _ref()[:5]})
    assert r.status_code == 422


def test_rollout_endpoint_step_change():
    r = client.post("/api/mpc/rollout", json={
        "x0": [0.0, 0.0], "n_steps": 50,
        "reference_before": [0.0, 0.0],
        "reference_after": [1.0, 0.0],
        "change_step": 15,
        "disturbance_kind": "sine",
        "disturbance_amplitude": 0.05,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["fallback_count"] == 0
    assert body["max_velocity_violation"] <= 1e-6
    assert body["max_acceleration_violation"] <= 1e-6
    assert len(body["steps"]) == 50
    assert abs(body["final_state"][0] - 1.0) < 0.08


def test_rollout_endpoint_infeasible_initial_state_reports_fallback():
    r = client.post("/api/mpc/rollout", json={
        "x0": [0.0, 2.45], "n_steps": 10,
        "reference_setpoint": [0.0, 0.0],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["fallback_count"] >= 1
    assert "initial_state_infeasible" in body["fallback_reasons"]
    # applied controls always bounded even in the infeasible regime
    assert body["max_acceleration_violation"] <= 1e-9
    # and braking recovers feasibility within the run
    assert abs(body["final_state"][1]) <= 2.0 + 1e-6


def test_rollout_requires_reference():
    r = client.post("/api/mpc/rollout", json={"x0": [0.0, 0.0]})
    assert r.status_code == 422
