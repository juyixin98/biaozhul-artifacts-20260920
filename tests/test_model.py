"""Tests 1-2: matrices must come from the model; state updates verified."""

import numpy as np
import pytest

from mpc.config import MPCConfig
from mpc.model import DoubleIntegrator


def test_discrete_matrices_are_model_exact_derived():
    cfg = MPCConfig(dt=0.1)
    m = DoubleIntegrator(cfg)
    expected_A = np.array([[1.0, 0.1], [0.0, 1.0]])
    expected_B = np.array([[0.005], [0.1]])
    assert np.allclose(m.Ad, expected_A)
    assert np.allclose(m.Bd, expected_B)
    # series discretization agrees with the analytic form for other dt
    cfg2 = MPCConfig(dt=0.25)
    m2 = DoubleIntegrator(cfg2)
    assert np.allclose(m2.Ad, [[1, 0.25], [0, 1]])
    assert np.allclose(m2.Bd.reshape(-1), [0.25 ** 2 / 2, 0.25])


def test_state_update_step_by_step_against_manual_formulas():
    cfg = MPCConfig(dt=0.1)
    m = DoubleIntegrator(cfg)
    p, v, u = 0.7, -0.4, 1.25
    x = np.array([p, v])
    w = np.array([0.01, -0.02])
    xn = m.step(x, u, disturbance=w)
    p_exp = p + cfg.dt * v + 0.5 * cfg.dt ** 2 * u + w[0]
    v_exp = v + cfg.dt * u + w[1]
    assert xn.shape == (2,)
    assert xn[0] == pytest.approx(p_exp, abs=1e-12)
    assert xn[1] == pytest.approx(v_exp, abs=1e-12)


def test_config_rejects_unsafe_braking_gain():
    with pytest.raises(ValueError):
        MPCConfig(k_brake=10.0)
    with pytest.raises(ValueError):
        MPCConfig(dt=0)
