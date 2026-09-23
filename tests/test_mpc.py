"""Tests 3-6: constraint activation, step change, fallback paths."""

import numpy as np
import pytest

from mpc.config import MPCConfig
from mpc.disturbance import DisturbanceSpec
from mpc.mpc import FallbackReason, MPCController
from mpc.simulation import constant_reference, rollout, step_reference


@pytest.fixture
def ctrl():
    return MPCController(MPCConfig())


def test_input_constraint_activates_at_saturation(ctrl):
    """Far set-point from rest: optimizer saturates at +a_max."""
    ref = constant_reference((10.0, 0.0), ctrl.config.horizon + 1)
    r = ctrl.solve([0.0, 0.0], ref)
    assert r.status == "ok"
    u = np.array(r.control_sequence)
    assert np.max(np.abs(u)) == pytest.approx(ctrl.config.a_max, abs=1e-5)
    assert int(np.sum(np.isclose(u, ctrl.config.a_max, atol=1e-4))) >= 3
    assert r.residuals["a_max_violation"] <= 1e-5
    assert r.residuals["v_max_violation"] <= 1e-5


def test_velocity_constraint_is_respected_near_bound(ctrl):
    """Starting at +v_max, any positive acceleration is infeasible at k=0:
    MPC must brake or hold; predicted velocities stay within the bound."""
    ref = constant_reference((10.0, 2.0), ctrl.config.horizon + 1)
    r = ctrl.solve([0.0, ctrl.config.v_max], ref)
    assert r.status == "ok"
    vels = np.array(r.predicted_states)[:, 1]
    assert np.max(vels) <= ctrl.config.v_max + 1e-6
    assert np.min(vels) >= -ctrl.config.v_max - 1e-6
    # first acceleration cannot be positive (would break v_max immediately)
    assert r.control <= 1e-8


def test_reference_step_change_closed_loop(ctrl):
    refs = step_reference((0.0, 0.0), (1.0, 0.0),
                          change_step=10, n_steps=60,
                          horizon=ctrl.config.horizon)
    out = rollout(ctrl, [0.0, 0.0], refs, 60,
                  disturbance=DisturbanceSpec(kind="sine", amplitude=0.05,
                                              frequency=0.25))
    assert out.fallback_count == 0
    assert out.max_velocity_violation <= 1e-6
    assert out.max_acceleration_violation <= 1e-6
    # after the change it tracks the new set-point (bounded steady offset
    # from the unmodeled additive disturbance; controller has no integrator)
    assert abs(out.final_state[0] - 1.0) < 0.08
    assert abs(out.final_state[1]) < 0.08


def test_infeasible_initial_state_uses_fallback_then_recovers(ctrl):
    """v0 = 2.45 > v_max: fallback braking brings it back feasible; no
    stale control is ever applied and |u| always <= a_max."""
    cfg = ctrl.config
    x = np.array([0.0, cfg.v_max + 0.45])
    u_prev = 0.0
    ref = constant_reference((0.0, 0.0), cfg.horizon + 1)
    saw_fallback = False
    for k in range(15):
        r = ctrl.solve(x, ref, u_prev=u_prev)
        assert np.isfinite(r.control) and abs(r.control) <= cfg.a_max + 1e-12
        if r.fallback:
            saw_fallback = True
            assert r.reason == FallbackReason.INITIAL_STATE_INFEASIBLE
            # fallback must brake against the over-speed direction
            assert np.sign(r.control) == -np.sign(x[1])
            # explicitly NOT a previously solved plan
            assert r.control_sequence == []
        x = ctrl.model.step(x, r.control)
        u_prev = r.control
    assert saw_fallback
    assert abs(x[1]) <= cfg.v_max + 1e-6


@pytest.mark.parametrize("mode,expected", [
    ("timeout", FallbackReason.SOLVER_TIMEOUT),
    ("infeasible", FallbackReason.SOLVER_INFEASIBLE),
    ("error", FallbackReason.SOLVER_ERROR),
    ("nonoptimal", FallbackReason.SOLVER_NON_OPTIMAL),
])
def test_solver_failures_return_conservative_fallback(ctrl, mode, expected):
    ref = constant_reference((1.0, 0.0), ctrl.config.horizon + 1)
    x0 = np.array([0.0, 0.0])
    # first establish a known good solution at state x0 (never reused later)
    good = ctrl.solve(x0, ref)
    assert good.status == "ok"

    ctrl.force_fail = mode
    r = ctrl.solve(x0, ref)
    ctrl.force_fail = None
    assert r.status == "fallback"
    assert r.fallback is True
    assert r.reason == expected
    assert "u" in r.reason_detail or mode != "error" or "error" in r.reason_detail
    # finite, bounded, braking law from CURRENT state (x0 at rest -> 0)
    assert np.isfinite(r.control)
    assert abs(r.control) <= ctrl.config.a_max
    assert r.control == pytest.approx(ctrl.fallback_control(x0))
    assert r.control_sequence == []  # no stale/unverified plan carried over


def test_fallback_never_reuses_previous_plan_under_motion(ctrl):
    """Even with a strong previous optimal control, a failure applies a
    state-derived brake, never the old u0."""
    ref = constant_reference((10.0, 0.0), ctrl.config.horizon + 1)
    x0 = np.array([0.0, 0.0])
    good = ctrl.solve(x0, ref)
    previous_u0 = good.control  # near +a_max
    assert abs(previous_u0) > 2.0

    ctrl.force_fail = "error"
    moving = np.array([1.0, 1.5])
    r = ctrl.solve(moving, ref, u_prev=previous_u0)
    ctrl.force_fail = None
    assert r.fallback
    assert r.control == pytest.approx(-ctrl.config.k_brake * moving[1])
    assert r.control != pytest.approx(previous_u0)


def test_natural_solver_timeout_is_classified():
    """A genuinely tiny wall-clock limit on a large QP must be classified
    as solver_timeout (exercises the real OSQP status string)."""
    cfg = MPCConfig(horizon=120, osqp_max_iter=1_000_000,
                    osqp_time_limit=1e-9)
    c = MPCController(cfg)
    ref = constant_reference((1.0, 0.0), 121)
    r = c.solve([0.0, 0.0], ref)
    assert r.status == "fallback"
    assert r.reason == FallbackReason.SOLVER_TIMEOUT
    assert "time limit" in (r.osqp_status or "")
    assert np.isfinite(r.control) and abs(r.control) <= cfg.a_max


def test_nonfinite_state_fallback(ctrl):
    ref = constant_reference((0.0, 0.0), ctrl.config.horizon + 1)
    r = ctrl.solve([np.nan, 0.0], ref)
    assert r.status == "fallback"
    assert r.reason == FallbackReason.NONFINITE_STATE
    assert np.isfinite(r.control)
