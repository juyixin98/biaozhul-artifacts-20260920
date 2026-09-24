"""MPC solver tests: tracking, constraint activation, fallbacks, freshness."""

import types

import numpy as np
import pytest

from app.mpc import BoundedMPC, MpcConfig, MpcStatus


@pytest.fixture
def mpc():
    return BoundedMPC(MpcConfig(horizon=20, dt=0.1))


def test_matrices_are_model_derived(mpc):
    # constraint matrix: 2N dynamics + N velocity + N input rows
    n_rows, n_cols = mpc._A_con.shape
    assert n_rows == 2 * mpc.N + 2 * mpc.N
    assert n_cols == mpc.N * 3
    # OSQP takes only the upper triangle of P; complete it to check symmetry
    P = mpc._P.toarray()
    Pfull = P + P.T - np.diag(np.diag(P))
    np.testing.assert_allclose(Pfull, Pfull.T, atol=1e-12)
    assert np.all(np.linalg.eigvalsh(Pfull) > 0)
    # terminal weight is the DARE stabilizing solution
    Qf, A, B, Qf_check = mpc.Qf, mpc.A, mpc.B, None
    R = np.array([[mpc.cfg.r_rate + mpc.cfg.r_input]])
    Qf_check = Qf
    # DARE fixed point: P = A'PA - A'PB(B'PB+R)^-1 B'PA + Q
    S = B.T @ Qf_check @ B + R
    fixed = A.T @ Qf_check @ A - A.T @ Qf_check @ B @ np.linalg.solve(
        S, B.T @ Qf_check @ A
    ) + mpc.Q
    np.testing.assert_allclose(fixed, Qf_check, atol=1e-9)


def test_unconstrained_tracking_solves_and_verifies(mpc):
    r = mpc.solve([0.0, 0.0], np.full(20, 1.0))
    assert r.status == MpcStatus.SOLVED
    assert not r.fallback
    assert r.control > 0  # accelerate towards the target
    assert r.residuals.dynamics < 1e-8
    assert r.residuals.state == 0.0
    assert r.residuals.input == 0.0
    assert len(r.predicted_states) == 20
    assert len(r.predicted_controls) == 20


def test_predicted_states_satisfy_dynamics_step_by_step(mpc):
    x0 = np.array([0.7, -0.4])
    r = mpc.solve(x0, np.linspace(0, 2, 20))
    assert r.status == MpcStatus.SOLVED
    X = np.array(r.predicted_states)
    U = np.array(r.predicted_controls)
    prev = x0
    for k in range(mpc.N):
        np.testing.assert_allclose(
            X[k], mpc.A @ prev + mpc.B.reshape(2) * U[k], atol=1e-7
        )
        prev = X[k]


def test_input_constraint_activates_at_max_brake(mpc):
    # moving too fast toward origin -> optimal first move is max deceleration
    r = mpc.solve([5.0, 1.5], np.zeros(20))
    assert r.status == MpcStatus.SOLVED
    assert r.predicted_controls[0] == pytest.approx(-1.0, abs=1e-6)
    assert max(abs(u) for u in r.predicted_controls) <= 1.0 + 1e-7
    assert r.residuals.input < 1e-9


def test_velocity_constraint_activates_and_is_respected(mpc):
    # starting exactly at v_max with target ahead: velocity must stay <= bound
    r = mpc.solve([0.0, mpc.cfg.v_max], np.full(20, 10.0))
    assert r.status == MpcStatus.SOLVED
    V = np.array(r.predicted_states)[:, 1]
    assert V.max() <= mpc.cfg.v_max + 1e-7
    assert V.max() == pytest.approx(mpc.cfg.v_max, abs=1e-6)


def test_infeasible_initial_velocity_returns_fallback(mpc):
    r = mpc.solve([0.0, 2.5], np.zeros(20))
    assert r.status == MpcStatus.INFEASIBLE_INITIAL_STATE
    assert r.fallback
    # conservative brake clipped to u_max
    assert r.control == -mpc.cfg.u_max
    assert "v_max" in r.reason
    assert r.predicted_states == []
    assert r.predicted_controls == []


def _fake_info(status: str):
    return types.SimpleNamespace(status=status, iter=42, run_time=0.001)


def _fake_result(status: str):
    return types.SimpleNamespace(info=_fake_info(status), x=None)


def test_solver_timeout_returns_fallback(mpc, monkeypatch):
    monkeypatch.setattr(
        "app.mpc.osqp.OSQP.solve",
        lambda self: _fake_result("run time limit reached"),
    )
    r = mpc.solve([0.0, 0.0], np.zeros(20))
    assert r.status == MpcStatus.SOLVER_TIMEOUT
    assert r.fallback
    assert r.control == 0.0  # brake from v=0
    assert "time limit" in r.reason


def test_solver_infeasible_returns_fallback(mpc, monkeypatch):
    monkeypatch.setattr(
        "app.mpc.osqp.OSQP.solve",
        lambda self: _fake_result("primal infeasible"),
    )
    r = mpc.solve([0.0, 1.0], np.zeros(20))
    assert r.status == MpcStatus.SOLVER_INFEASIBLE
    assert r.fallback
    # ideal brake is -v/dt = -10, conservatively clipped to -u_max
    assert r.control == -mpc.cfg.u_max


def test_solver_exception_returns_fallback(mpc, monkeypatch):
    def boom(self):
        raise RuntimeError("simulated internal solver failure")

    monkeypatch.setattr("app.mpc.osqp.OSQP.solve", boom)
    r = mpc.solve([0.0, 0.5], np.zeros(20))
    assert r.status == MpcStatus.SOLVER_ERROR
    assert r.fallback
    assert "simulated internal solver failure" in r.reason
    # ideal brake -v/dt = -5, conservatively clipped to -u_max
    assert r.control == -mpc.cfg.u_max


def test_failed_verification_returns_fallback(mpc, monkeypatch):
    from app.mpc import Residuals

    monkeypatch.setattr(
        mpc,
        "_verify",
        lambda x0, X, U: Residuals(dynamics=0.0, state=0.1, input=0.0),
    )
    r = mpc.solve([0.0, 0.0], np.zeros(20))
    assert r.status == MpcStatus.VERIFICATION_FAILED
    assert r.fallback
    assert r.predicted_states == []


def test_no_stale_unverified_control_is_reused(mpc, monkeypatch):
    """A prior good solution must never leak out after a later failure."""
    good = mpc.solve([0.0, 0.0], np.full(20, 3.0))
    assert good.status == MpcStatus.SOLVED
    previous_optimal = good.control
    assert previous_optimal > 0

    monkeypatch.setattr(
        "app.mpc.osqp.OSQP.solve",
        lambda self: _fake_result("run time limit reached"),
    )
    bad = mpc.solve([0.0, 1.0], np.full(20, 3.0))
    assert bad.fallback
    assert bad.control != previous_optimal
    assert bad.control == mpc.fallback_control(1.0)
    assert bad.predicted_controls == []


def test_reference_shorter_than_horizon_is_held(mpc):
    r = mpc.solve([0.0, 0.0], [1.0])
    assert r.status == MpcStatus.SOLVED
    assert len(r.predicted_states) == mpc.N


def test_real_time_limit_attempt(monkeypatch):
    """Best-effort genuine timeout: huge horizon + extreme time budget.

    If the machine is so fast OSQP still converges, the outcome must at least
    be a valid verified solution (never garbage).
    """
    big = BoundedMPC(MpcConfig(horizon=500, time_limit=1e-12, max_iter=10_000_000))
    r = big.solve([0.0, 0.0], np.zeros(500))
    assert r.status in {MpcStatus.SOLVED, MpcStatus.SOLVER_TIMEOUT}
    if r.fallback:
        assert r.status == MpcStatus.SOLVER_TIMEOUT
