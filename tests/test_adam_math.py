"""Adam math tests: shard-slice rule vs an independent textbook formula.

The oracle here is a hand-expanded formula evaluated element-by-element,
not ``reference_adam.ReferenceAdam`` (that class gets its own parity tests),
so agreement is not tautological.
"""

from __future__ import annotations

import numpy as np

from adam_shard.adam import AdamConfig, AdamShardState, adam_step_slice


def test_first_step_matches_textbook_bias_corrected_formula():
    cfg = AdamConfig()
    param = np.array([1.0, -2.0, 0.5])
    grad = np.array([0.3, -0.7, 0.0])
    state = AdamShardState.zeros(3, np.float64)

    new_param, new_m, new_v, step = adam_step_slice(param, grad, state, cfg)
    assert step == 1

    # Textbook: m=g (beta1*0+(1-beta1)g)/(1-beta1); v=g^2 analogously.
    m_hat = grad
    v_hat = grad * grad
    expected = param - cfg.lr * m_hat / (np.sqrt(v_hat) + cfg.eps)
    np.testing.assert_allclose(new_param, expected, rtol=1e-14, atol=1e-15)
    np.testing.assert_allclose(new_m, (1 - cfg.beta1) * grad, rtol=1e-14)
    np.testing.assert_allclose(new_v, (1 - cfg.beta2) * grad * grad, rtol=1e-14)


def test_multi_step_matches_manual_recurrence():
    cfg = AdamConfig(lr=0.02, beta1=0.8, beta2=0.95, eps=1e-7)

    seed_rng = np.random.default_rng(42)
    g = seed_rng.standard_normal(6)
    p = seed_rng.standard_normal(6)
    state = AdamShardState.zeros(6, np.float64)

    # Path under test: repeated slice updates.
    for _ in range(7):
        p, m_new, v_new, step = adam_step_slice(p, g, state, cfg)
        state = AdamShardState(m_new, v_new, step)

    # Independent path: same recurrence written out by hand, fresh state.
    seed_rng = np.random.default_rng(42)
    g_ref = seed_rng.standard_normal(6)
    p_ref = seed_rng.standard_normal(6)
    m_ref = np.zeros(6)
    v_ref = np.zeros(6)
    for t in range(1, 8):
        m_ref = cfg.beta1 * m_ref + (1 - cfg.beta1) * g_ref
        v_ref = cfg.beta2 * v_ref + (1 - cfg.beta2) * g_ref * g_ref
        p_ref = p_ref - cfg.lr * (m_ref / (1 - cfg.beta1**t)) / (
            np.sqrt(v_ref / (1 - cfg.beta2**t)) + cfg.eps
        )
    np.testing.assert_allclose(p, p_ref, rtol=1e-12, atol=1e-14)
    assert step == 7


def test_adam_does_not_mutate_inputs():
    cfg = AdamConfig()
    param = np.array([1.0, 2.0])
    grad = np.array([0.1, 0.1])
    state = AdamShardState(np.array([0.2, 0.2]), np.array([0.05, 0.05]), 3)
    param_copy, m_copy = param.copy(), state.m.copy()
    adam_step_slice(param, grad, state, cfg)
    np.testing.assert_array_equal(param, param_copy)
    np.testing.assert_array_equal(state.m, m_copy)


def test_shape_mismatch_rejected():
    cfg = AdamConfig()
    try:
        adam_step_slice(np.zeros(3), np.zeros(2), AdamShardState.zeros(3, np.float64), cfg)
    except ValueError:
        return
    raise AssertionError("expected shape mismatch ValueError")


def test_config_validation():
    import pytest

    with pytest.raises(ValueError):
        AdamConfig(lr=0)
    with pytest.raises(ValueError):
        AdamConfig(beta1=1.0)
    with pytest.raises(ValueError):
        AdamConfig(eps=0)
