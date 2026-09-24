"""Closed-loop simulation tests: step consistency, residuals, disturbance."""

import numpy as np
import pytest

from app.mpc import BoundedMPC, MpcConfig, MpcStatus
from app.simulation import assert_step_consistency, simulate


@pytest.fixture
def mpc():
    return BoundedMPC(MpcConfig(horizon=20, dt=0.1))


def test_regulation_to_origin(mpc):
    report = simulate(mpc, [1.0, 0.0], [0.0], steps=40)
    assert_step_consistency(mpc, report)
    assert report["n_fallback"] == 0
    assert set(report["statuses"]) == {MpcStatus.SOLVED.value}
    p_end, v_end = report["final_state"]
    assert abs(p_end) < 0.02
    assert abs(v_end) < 0.02


def test_reference_step_change_is_tracked(mpc):
    # constant 0 then abrupt jump to +2 at step 30
    ref = [0.0] * 30 + [2.0] * 40
    report = simulate(mpc, [0.0, 0.0], ref, steps=70)
    assert_step_consistency(mpc, report)
    assert report["n_fallback"] == 0
    states = np.array([r["state_after"] for r in report["records"]])
    # long before the jump (well outside the N=20 preview window): at rest
    assert abs(states[5, 0]) < 0.05
    assert abs(states[5, 1]) < 0.05
    # the receding horizon sees the upcoming step inside its N-step preview,
    # so movement begins shortly before index 30; after the jump it tracks +2
    assert states[35, 0] > 1.0
    assert abs(states[-1, 0] - 2.0) < 0.05      # tracks new setpoint
    # velocity and acceleration never violate limits at any recorded step
    assert np.max(np.abs(states[:, 1])) <= mpc.cfg.v_max + 1e-9
    controls = np.array([r["control"] for r in report["records"]])
    assert np.max(np.abs(controls)) <= mpc.cfg.u_max + 1e-9


def test_constraint_residuals_zero_for_valid_rollout(mpc):
    report = simulate(mpc, [0.0, 1.9], [5.0], steps=30)
    assert_step_consistency(mpc, report)
    for rec in report["records"]:
        assert rec["velocity_violation"] == 0.0
        assert rec["input_violation"] == 0.0
        assert rec["solve_residuals"]["dynamics"] < 1e-6
        assert rec["solve_residuals"]["state"] == 0.0
        assert rec["solve_residuals"]["input"] == 0.0


def test_deterministic_disturbance_rejected_but_bounded(mpc):
    # disturbance the controller does not know about: it must still keep the
    # loop safe (velocity bounded, commands bounded, deterministic repeat)
    r1 = simulate(mpc, [0.0, 0.0], [1.0], steps=40,
                  disturbance_amplitude=0.4, disturbance_seed=5)
    r2 = simulate(mpc, [0.0, 0.0], [1.0], steps=40,
                  disturbance_amplitude=0.4, disturbance_seed=5)
    assert r1["records"] == r2["records"]
    assert_step_consistency(mpc, r1)
    for rec in r1["records"]:
        assert rec["velocity_violation"] == 0.0
        assert rec["input_violation"] == 0.0
    # and the recorded disturbance really comes from the deterministic source
    from app.disturbance import deterministic_acceleration
    for rec in r1["records"]:
        assert rec["disturbance"] == pytest.approx(
            deterministic_acceleration(rec["step"], 0.4, 5)
        )


def test_infeasible_initial_state_fallback_then_recovery(mpc):
    # v0 above the bound: first step is fallback brake; the controller must
    # report the reason, still never emit a stale command, and eventually
    # solve again once the state is feasible.
    report = simulate(mpc, [0.0, 2.5], [0.0], steps=25)
    assert_step_consistency(mpc, report)
    first = report["records"][0]
    assert first["status"] == MpcStatus.INFEASIBLE_INITIAL_STATE.value
    assert first["fallback"]
    assert first["control"] == -mpc.cfg.u_max
    # after one -u_max brake v = 2.5 - 0.1*1 = 2.4 still infeasible...
    # controller keeps braking conservatively each step until |v|<=v_max
    fallback_steps = [r for r in report["records"] if r["fallback"]]
    assert len(fallback_steps) >= 3
    for rec in fallback_steps:
        assert "v_max" in rec["reason"] or "OSQP" in rec["reason"]
    # late steps return to verified MPC
    assert report["records"][-1]["status"] == MpcStatus.SOLVED.value


def test_solver_failure_during_simulation_is_fallback(mpc, monkeypatch):
    import app.mpc as mpc_mod
    import types

    calls = {"n": 0}
    real_init = mpc_mod.osqp.OSQP.setup

    def flaky_setup(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated solver crash mid-simulation")
        return real_init(self, *a, **k)

    monkeypatch.setattr(mpc_mod.osqp.OSQP, "setup", flaky_setup)
    report = simulate(mpc, [1.0, 0.0], [0.0], steps=8)
    assert_step_consistency(mpc, report)
    statuses = [r["status"] for r in report["records"]]
    assert statuses[2] == MpcStatus.SOLVER_ERROR.value
    assert report["records"][2]["fallback"]
    # steps before/after remain normal
    assert statuses[0] == MpcStatus.SOLVED.value
    assert statuses[-1] == MpcStatus.SOLVED.value
