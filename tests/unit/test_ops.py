"""Unit tests for op VJP rules against hand-computed results."""

from __future__ import annotations

import numpy as np
import pytest

from app.core import ops


@pytest.mark.unit
def test_linear_vjp_matches_hand_calculation() -> None:
    x = np.array([[1.0, 2.0], [3.0, 4.0]])
    w = np.array([[0.5, -1.0], [2.0, 1.0]])
    gout = np.array([[1.0, 0.0], [0.0, 1.0]])
    grads = ops.backward(ops.LINEAR, {}, [x, w], None, gout,
                         input_shapes=[x.shape, w.shape])
    # grad_x = gout @ w.T ; grad_w = x.T @ gout
    assert np.allclose(grads[0], gout @ w.T)
    assert np.allclose(grads[1], x.T @ gout)
    assert grads[0].shape == x.shape
    assert grads[1].shape == w.shape


@pytest.mark.unit
def test_relu_vjp_gates_gradient() -> None:
    x = np.array([[-1.0, 2.0], [0.5, -0.25]])
    gout = np.ones_like(x)
    (gx,) = ops.backward(ops.RELU, {}, [x], None, gout)
    assert np.array_equal(gx, (x > 0).astype(float))
    assert gx.tolist() == [[0.0, 1.0], [1.0, 0.0]]


@pytest.mark.unit
def test_mul_vjp_uses_other_operand() -> None:
    a = np.array([2.0, 3.0])
    b = np.array([4.0, 5.0])
    gout = np.array([1.0, 1.0])
    ga, gb = ops.backward(ops.MUL, {}, [a, b], None, gout)
    assert np.array_equal(ga, b)
    assert np.array_equal(gb, a)


@pytest.mark.unit
def test_dropout_vjp_applies_replayed_mask() -> None:
    x = np.ones(3)
    mask = np.array([0.0, 4.0 / 3, 4.0 / 3])  # p = 0.25 keep scaling
    gout = np.ones(3)
    (gx,) = ops.backward(ops.DROPOUT, {"p": 0.25}, [x], None, gout,
                         mask_for_dropout=mask)
    assert np.array_equal(gx, mask)


@pytest.mark.unit
def test_dropout_vjp_without_mask_is_input_error() -> None:
    with pytest.raises(Exception):
        ops.backward(ops.DROPOUT, {"p": 0.25}, [np.ones(2)], None, np.ones(2))


@pytest.mark.unit
def test_reduce_sum_vjp_broadcasts_seed() -> None:
    x = np.zeros((2, 3))
    gout = np.array([[7.0]])
    (gx,) = ops.backward(ops.REDUCE_SUM, {}, [None], None, gout,
                         input_shapes=[(2, 3)])
    assert gx.shape == (2, 3)
    assert np.all(gx == 7.0)


@pytest.mark.unit
def test_forward_external_emits_only_when_allowed() -> None:
    class CountingRng:
        def __init__(self) -> None:
            self.emits = 0

        def note_external_emit(self) -> None:
            self.emits += 1

    rng = CountingRng()
    x = np.array([1.0, 2.0])
    ops.forward(ops.EXTERNAL, {}, [x], rng, "ext", emit=True)
    ops.forward(ops.EXTERNAL, {}, [x], rng, "ext", emit=False)
    assert rng.emits == 1
