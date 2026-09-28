"""Layer 3a: pure optimiser row rules, asserted against hand calculation."""

from __future__ import annotations

import numpy as np
import pytest

from sparse_embedding.config import OptimizerConfig, OptimizerName
from sparse_embedding.optimizer import step_rows


def test_sgd_momentum_first_step_matches_hand_formula():
    # w=[1,2], v=[0,0], g=[3,4], lr=0.1, mu=0.9
    # v1 = g = [3,4]; w1 = w - 0.1*v1 = [0.7, 1.6]
    w = np.array([[1.0, 2.0]])
    v = np.zeros((1, 2))
    g = np.array([[3.0, 4.0]])
    cfg = OptimizerConfig(name=OptimizerName.SGD_MOMENTUM, lr=0.1, momentum=0.9)

    w1, v1 = step_rows(w, v, g, cfg)
    np.testing.assert_allclose(v1, [[3.0, 4.0]])
    np.testing.assert_allclose(w1, [[0.7, 1.6]])


def test_sgd_momentum_second_step_accumulates_history():
    # After first step v=[3,4]; second gradient g=[1,1]:
    # v2 = 0.9*[3,4] + [1,1] = [3.7, 4.6]
    # w2 = [0.7,1.6] - 0.1*[3.7,4.6] = [0.33, 1.14]
    w = np.array([[0.7, 1.6]])
    v = np.array([[3.0, 4.0]])
    g = np.array([[1.0, 1.0]])
    cfg = OptimizerConfig(name=OptimizerName.SGD_MOMENTUM, lr=0.1, momentum=0.9)

    w2, v2 = step_rows(w, v, g, cfg)
    np.testing.assert_allclose(v2, [[3.7, 4.6]])
    np.testing.assert_allclose(w2, [[0.33, 1.14]])


def test_plain_sgd_ignores_momentum_and_keeps_zero_buffer():
    w = np.array([[1.0, 1.0]])
    v = np.array([[7.0, 7.0]])  # any stale buffer must not influence plain SGD
    g = np.array([[2.0, -3.0]])
    cfg = OptimizerConfig(name=OptimizerName.SGD, lr=0.5, momentum=0.0)

    w1, v1 = step_rows(w, v, g, cfg)
    np.testing.assert_allclose(w1, [[0.0, 2.5]])
    np.testing.assert_array_equal(v1, np.zeros_like(v1))


def test_weight_decay_is_coupled_into_gradient():
    # g_eff = g + wd*w = [1,1] + 0.1*[1,2] = [1.1, 1.2]
    # v = g_eff; w1 = w - lr*v = [1,2] - 0.1*[1.1,1.2] = [0.89, 1.88]
    w = np.array([[1.0, 2.0]])
    v = np.zeros((1, 2))
    g = np.array([[1.0, 1.0]])
    cfg = OptimizerConfig(
        name=OptimizerName.SGD_MOMENTUM, lr=0.1, momentum=0.9, weight_decay=0.1
    )
    w1, v1 = step_rows(w, v, g, cfg)
    np.testing.assert_allclose(v1, [[1.1, 1.2]])
    np.testing.assert_allclose(w1, [[0.89, 1.88]])


def test_inputs_are_not_mutated():
    w = np.array([[1.0, 2.0]])
    v = np.zeros((1, 2))
    g = np.array([[3.0, 4.0]])
    w0, v0, g0 = w.copy(), v.copy(), g.copy()
    cfg = OptimizerConfig(name=OptimizerName.SGD_MOMENTUM, lr=0.1, momentum=0.9)
    step_rows(w, v, g, cfg)
    np.testing.assert_array_equal(w, w0)
    np.testing.assert_array_equal(v, v0)
    np.testing.assert_array_equal(g, g0)


def test_shape_mismatch_is_rejected():
    cfg = OptimizerConfig(name=OptimizerName.SGD_MOMENTUM, lr=0.1, momentum=0.9)
    with pytest.raises(ValueError):
        step_rows(np.zeros((2, 2)), np.zeros((2, 2)), np.zeros((3, 2)), cfg)
