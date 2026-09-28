"""SGD optimizer step on fp32 master weights."""

import numpy as np

from amptrain.config import ModelConfig, OptimizerConfig
from amptrain.optimizer import sgd_step
from amptrain.tensors import MasterWeights, OptimizerState


def _setup(lr=0.1, momentum=0.0):
    rng = np.random.default_rng(3)
    model = ModelConfig(in_dim=3, hidden_dim=4, out_dim=1)
    master = MasterWeights.initialize(model, rng)
    state = OptimizerState.zeros(model)
    cfg = OptimizerConfig(lr=lr, momentum=momentum, weight_decay=0.0)
    grads = {name: np.ones_like(master.matrices[name]) for name in ("W1", "W2")}
    return master, state, cfg, grads


def test_plain_sgd_minus_lr_times_gradient():
    master, state, cfg, grads = _setup(lr=0.1, momentum=0.0)
    new_master, new_state = sgd_step(master, state, grads, cfg, 0.1)
    for name in ("W1", "W2"):
        np.testing.assert_allclose(
            new_master.matrices[name], master.matrices[name] - 0.1
        )
        np.testing.assert_array_equal(new_state.momentum[name], grads[name])


def test_momentum_accumulates_across_steps():
    master, state, cfg, grads = _setup(lr=0.1, momentum=0.9)
    master, state = sgd_step(master, state, grads, cfg, 0.1)
    first_w1 = master.matrices["W1"].copy()
    master, state = sgd_step(master, state, grads, cfg, 0.1)
    # second velocity = 0.9*1 + 1 = 1.9
    np.testing.assert_allclose(
        master.matrices["W1"], first_w1 - 0.1 * 1.9, rtol=1e-6
    )


def test_optimizer_does_not_mutate_inputs():
    master, state, cfg, grads = _setup()
    w_before = master.matrices["W1"].copy()
    v_before = state.momentum["W1"].copy()
    g_before = grads["W1"].copy()
    sgd_step(master, state, grads, cfg, 0.1)
    np.testing.assert_array_equal(master.matrices["W1"], w_before)
    np.testing.assert_array_equal(state.momentum["W1"], v_before)
    np.testing.assert_array_equal(grads["W1"], g_before)


def test_everything_is_fp32():
    master, state, cfg, grads = _setup()
    new_master, new_state = sgd_step(master, state, grads, cfg, 0.1)
    for name in ("W1", "W2"):
        assert new_master.matrices[name].dtype == np.float32
        assert new_state.momentum[name].dtype == np.float32
