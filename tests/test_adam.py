"""Unit tests for Adam state and the bias-corrected update."""
from __future__ import annotations

import numpy as np
import pytest

from adam_shards.adam import Adam, AdamConfig, OptimState


def _closed_form_step(p, g, m, v, t, cfg):
    m = cfg.beta1 * m + (1 - cfg.beta1) * g
    v = cfg.beta2 * v + (1 - cfg.beta2) * g * g
    m_hat = m / (1 - cfg.beta1 ** (t + 1))
    v_hat = v / (1 - cfg.beta2 ** (t + 1))
    p_new = p - cfg.lr * m_hat / (np.sqrt(v_hat) + cfg.eps)
    return p_new, m, v


def test_first_step_matches_closed_form_bias_corrected_adam():
    cfg = AdamConfig(lr=0.1, beta1=0.9, beta2=0.999, eps=1e-8)
    adam = Adam(cfg)
    p = {"w": np.array([0.5, -0.25, 0.1])}
    g = {"w": np.array([0.1, -0.4, 2.0])}
    state = OptimState({"w": (3,)})
    adam.step(p, state, g)
    expect_p, expect_m, expect_v = _closed_form_step(
        np.array([0.5, -0.25, 0.1]), g["w"], np.zeros(3), np.zeros(3), 0, cfg
    )
    np.testing.assert_allclose(p["w"], expect_p, rtol=0, atol=1e-14)
    rec = state.get("w")
    np.testing.assert_allclose(rec.m, expect_m, rtol=0, atol=1e-14)
    np.testing.assert_allclose(rec.v, expect_v, rtol=0, atol=1e-14)
    assert rec.step == 1


def test_two_steps_keep_moments_and_step_aligned():
    cfg = AdamConfig()
    adam = Adam(cfg)
    params = {"w": np.array([0.3, -0.7])}
    state = OptimState({"w": (2,)})
    adam.step(params, state, {"w": np.array([0.2, 0.5])})
    adam.step(params, state, {"w": np.array([-0.1, 0.8])})
    rec = state.get("w")
    assert rec.step == 2
    # m and v must remain same shape and internally consistent.
    assert rec.m.shape == rec.v.shape == (2,)
    assert state.global_step() == 2


def test_load_snapshot_restores_exact_triple():
    state = OptimState({"a": (2,), "b": (3,)})
    snap = {
        "a": (np.ones(2), np.full(2, 0.5), 7),
        "b": (np.zeros(3), np.ones(3), 7),
    }
    from adam_shards.adam import MomentRecord
    state.load_snapshot({k: MomentRecord(*v) for k, v in snap.items()})
    assert state.global_step() == 7
    np.testing.assert_array_equal(state.get("a").m, np.ones(2))
    np.testing.assert_array_equal(state.get("b").v, np.ones(3))


def test_global_step_detects_disagreement():
    state = OptimState({"a": (2,), "b": (2,)})
    state.set("a", np.zeros(2), np.zeros(2), 1)
    state.set("b", np.zeros(2), np.zeros(2), 2)
    with pytest.raises(ValueError, match="steps disagree"):
        state.global_step()


def test_optimizer_rejects_name_set_mismatch():
    adam = Adam(AdamConfig())
    state = OptimState({"w": (2,)})
    with pytest.raises(KeyError):
        adam.step({"w": np.zeros(2), "x": np.zeros(1)}, state,
                  {"w": np.zeros(2)})


def test_config_validation_rejects_bad_hyperparams():
    with pytest.raises(ValueError):
        AdamConfig(lr=0).validate()
    with pytest.raises(ValueError):
        AdamConfig(beta1=1.0).validate()
    with pytest.raises(ValueError):
        AdamConfig(eps=0).validate()
