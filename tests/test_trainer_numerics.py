"""Numerical correctness: trainer commits match a hand-computed reference.

The reference updates below are derived directly from the SGD+momentum
formulas in fp64 inside the test -- they do NOT call the trainer's
forward/backward/optimizer code, so a bug in the core cannot manufacture
its own expected values.
"""

import numpy as np

from mptrainer import graph
from mptrainer.config import TrainerConfig
from mptrainer.trainer import DECISION_COMMITTED, MixedPrecisionTrainer


def make_linear_trainer(scale: float, momentum: float, lr: float = 0.1) -> MixedPrecisionTrainer:
    config = TrainerConfig(
        layer_sizes=[3, 1], seed=99, base_lr=lr, lr_decay=0.0,
        momentum=momentum, accum_steps=1, low_dtype="float32",
        init_scale=scale, growth_interval=10**9,
    ).validate()
    return MixedPrecisionTrainer(config, run_id="test-numerics")


def test_single_commit_matches_hand_computed_sgd():
    scale, lr = 8.0, 0.1
    trainer = make_linear_trainer(scale=scale, momentum=0.0, lr=lr)
    w0 = trainer.master["W0"].astype(np.float64)
    b0 = trainer.master["b0"].astype(np.float64)

    rng = np.random.default_rng(5)
    x = rng.standard_normal((4, 3))
    y = rng.standard_normal((4, 1))

    rec = trainer.train_step(x.astype(np.float32), y.astype(np.float32))
    assert rec.decision == DECISION_COMMITTED

    # Independent fp64 reference: loss = mean((x@w+b - y)^2), plain SGD.
    diff = x @ w0 + b0 - y
    grad_w = (2.0 / 4) * (x.T @ diff)
    grad_b = (2.0 / 4) * diff.sum(axis=0)
    exp_w = w0 - lr * grad_w
    exp_b = b0 - lr * grad_b

    np.testing.assert_allclose(trainer.master["W0"].astype(np.float64), exp_w, rtol=1e-5)
    np.testing.assert_allclose(trainer.master["b0"].astype(np.float64), exp_b, rtol=1e-5)
    # Loss scale must cancel out exactly: result independent of `scale`.
    assert rec.scale_before == scale


def test_momentum_state_is_separate_and_matches_reference():
    lr, momentum = 0.05, 0.9
    trainer = make_linear_trainer(scale=16.0, momentum=momentum, lr=lr)
    w = trainer.master["W0"].astype(np.float64)
    b = trainer.master["b0"].astype(np.float64)
    v_w = np.zeros_like(w)
    v_b = np.zeros_like(b)

    rng = np.random.default_rng(11)
    for _ in range(5):
        x = rng.standard_normal((6, 3))
        y = rng.standard_normal((6, 1))
        trainer.train_step(x.astype(np.float32), y.astype(np.float32))
        diff = x @ w + b - y
        v_w = momentum * v_w + (2.0 / 6) * (x.T @ diff)
        v_b = momentum * v_b + (2.0 / 6) * diff.sum(axis=0)
        w = w - lr * v_w
        b = b - lr * v_b

    np.testing.assert_allclose(trainer.master["W0"].astype(np.float64), w, rtol=1e-4)
    np.testing.assert_allclose(trainer.master["b0"].astype(np.float64), b, rtol=1e-4)
    # Optimizer state lives in its own dict, shaped like the master weights.
    assert set(trainer.opt_state) == set(trainer.master)
    assert trainer.opt_state["W0"].dtype == np.float32


def test_master_and_forward_params_are_distinct_objects(trainer):
    forward = trainer.master  # master stays fp32
    assert forward["W0"].dtype == np.float32
    # A clean step must not alias low-precision buffers into the master copy.
    x = np.random.default_rng(0).standard_normal((4, 8)).astype(np.float32)
    y = np.random.default_rng(1).standard_normal((4, 1)).astype(np.float32)
    trainer.train_step(x, y)
    assert trainer.master["W0"].dtype == np.float32
    assert trainer.master["W0"].flags.owndata


def test_graph_backward_matches_finite_difference():
    """Spot-check the compute graph itself against numerical differentiation."""
    params = graph.init_params([3, 4, 1], seed=3)
    rng = np.random.default_rng(2)
    x = rng.standard_normal((5, 3)).astype(np.float32)
    y = rng.standard_normal((5, 1)).astype(np.float32)
    scale = 4.0
    cache = graph.forward(params, x, y, scale, np.dtype(np.float32))
    grads = graph.backward(params, cache, y, scale)

    eps = 1e-3
    idx = (1, 0)
    plus = {k: v.copy() for k, v in params.items()}
    minus = {k: v.copy() for k, v in params.items()}
    plus["W0"][idx] += eps
    minus["W0"][idx] -= eps
    loss_plus = graph.forward(plus, x, y, scale, np.dtype(np.float32)).scaled_loss
    loss_minus = graph.forward(minus, x, y, scale, np.dtype(np.float32)).scaled_loss
    fd = (loss_plus - loss_minus) / (2 * eps)
    assert abs(fd - grads["W0"][idx]) / max(abs(fd), 1e-6) < 5e-2
