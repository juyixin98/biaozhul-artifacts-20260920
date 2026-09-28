"""Tests for training state: real SGD convergence, versioning and locations."""
from __future__ import annotations

import numpy as np

from tensor_backend.tensor import Tensor
from tensor_backend.training import TrainingState


def test_linear_sgd_converges(linear_problem):
    x, y, w_true, b_true = linear_problem
    state = TrainingState(learning_rate=0.05)
    state.init_linear(3, seed=0)
    losses = state.train(x, y, 300)
    # Loss must decrease substantially.
    assert losses[-1] < 0.01 * losses[0]
    # Recovered parameters close to the ground-truth generative coefficients.
    w = state.get("W").value.materialize().reshape(-1)
    b = state.get("b").value.materialize().reshape(-1)
    np.testing.assert_allclose(w, w_true.reshape(-1), atol=0.05)
    np.testing.assert_allclose(b, [b_true], atol=0.05)


def test_version_increments_and_records_request(linear_problem):
    x, y, _, _ = linear_problem
    state = TrainingState()
    state.init_linear(3)
    state.sgd_step(x, y, location="unit-test", request_id="req-abc")
    assert state.version == 1
    record = state.history[-1]
    assert record.request_id == "req-abc"
    assert record.location == "unit-test"


def test_predict_uses_broadcast_zero_stride_bias():
    state = TrainingState()
    state.add_parameter("W", Tensor.from_values(np.eye(3)))
    state.add_parameter("b", Tensor.from_values([10.0, 20.0, 30.0]))
    x = Tensor.from_values(np.ones((2, 3)))
    pred = state.predict(x)
    # Each row gets the bias broadcast across a leading zero-stride axis.
    np.testing.assert_array_equal(pred.materialize(), [[11, 21, 31], [11, 21, 31]])
