"""Unit tests for the model and optimizer."""

import numpy as np
import pytest

from checkpoint_service.model import LinearModel
from checkpoint_service.optimizer import MomentumSgd


@pytest.mark.unit
def test_initialization_is_deterministic() -> None:
    m1 = LinearModel(5, seed=7)
    m2 = LinearModel(5, seed=7)
    np.testing.assert_array_equal(m1.W, m2.W)
    assert m1.b == m2.b


@pytest.mark.unit
def test_predict_and_gradient_shapes() -> None:
    model = LinearModel(4, seed=1)
    X = np.ones((10, 4), dtype=np.float32)
    y = np.zeros(10, dtype=np.float32)
    loss, dW, db = model.loss_and_grad(X, y, l2=0.0)
    assert isinstance(loss, float)
    assert dW.shape == (4,)
    assert np.ndim(db) == 0


@pytest.mark.unit
def test_gradient_matches_finite_difference() -> None:
    rng = np.random.default_rng(0)
    n, d = 20, 3
    X = rng.standard_normal((n, d)).astype(np.float32)
    y = rng.standard_normal(n).astype(np.float32)
    model = LinearModel(d, seed=3)
    l2 = 0.01
    loss, dW, db = model.loss_and_grad(X, y, l2)

    eps = 1e-3
    for j in range(d):
        model.W[j] += eps
        lp, _, _ = model.loss_and_grad(X, y, l2)
        model.W[j] -= 2 * eps
        lm, _, _ = model.loss_and_grad(X, y, l2)
        model.W[j] += eps
        fd = (lp - lm) / (2 * eps)
        assert abs(fd - float(dW[j])) < 1e-2

    model.b += np.float32(eps)
    lp, _, _ = model.loss_and_grad(X, y, l2)
    model.b -= np.float32(2 * eps)
    lm, _, _ = model.loss_and_grad(X, y, l2)
    assert abs(((lp - lm) / (2 * eps)) - float(db)) < 1e-2


@pytest.mark.unit
def test_momentum_changes_velocity_and_decreases_loss_on_linear_data() -> None:
    rng = np.random.default_rng(5)
    X = rng.standard_normal((64, 2)).astype(np.float32)
    true_w = np.array([1.5, -2.0], dtype=np.float32)
    y = X @ true_w + 0.1
    model = LinearModel(2, seed=9)
    opt = MomentumSgd(2, lr=0.05, momentum=0.9)

    initial, _, _ = model.loss_and_grad(X, y, 0.0)
    for _ in range(100):
        _, dW, db = model.loss_and_grad(X, y, 0.0)
        model.W, model.b = opt.step(model.W, model.b, dW, db)
    final, _, _ = model.loss_and_grad(X, y, 0.0)
    assert final < initial * 0.01
    # Velocity is nonzero: state that must be checkpointed.
    assert np.any(opt.vW != 0.0)
