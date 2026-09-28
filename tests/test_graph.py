"""Backward pass correctness via finite differences and name-keyed identity."""

from __future__ import annotations

import numpy as np

from adam_shard.graph import MLPModule, mse_loss_and_grad
from adam_shard.layout import ParamLayout
from adam_shard.verification import finite_difference_gradient_check, STATUS_FAIL


def test_analytic_gradients_match_finite_differences(spec, dataset):
    model = MLPModule(spec, dtype=np.float64, seed=7)
    entry = finite_difference_gradient_check(model, dataset["x"], dataset["y"], eps=1e-6, seed=1)
    assert entry.status != STATUS_FAIL, entry.detail
    assert entry.detail["probes"] > 0


def test_backward_returns_stable_names(spec, dataset):
    model = MLPModule(spec, dtype=np.float64, seed=7)
    pred = model.forward(dataset["x"])
    _, grad_out = mse_loss_and_grad(pred, dataset["y"])
    grads = model.backward(dataset["x"], grad_out)
    assert set(grads) == {tid.name for tid in spec.parameter_ids()}
    for tid in spec.parameter_ids():
        assert grads[tid.name].shape == tid.shape


def test_install_rejects_unknown_parameter(spec):
    model = MLPModule(spec, dtype=np.float64, seed=7)
    bad = model.snapshot()
    bad["layers.0.weight"] = bad["layers.0.weight"].reshape(-1)
    try:
        model.install(bad)
    except ValueError:
        pass
    else:
        raise AssertionError("expected shape mismatch rejection")

    extra = model.snapshot()
    extra["intruder.weight"] = np.zeros(3)
    try:
        model.install(extra)
    except ValueError:
        pass
    else:
        raise AssertionError("expected unknown parameter rejection")


def test_two_seeds_produce_different_initial_params(spec):
    a = MLPModule(spec, dtype=np.float64, seed=1).snapshot()
    b = MLPModule(spec, dtype=np.float64, seed=2).snapshot()
    assert not np.allclose(a["layers.0.weight"], b["layers.0.weight"])


def test_loss_decreases_with_gradient_descent_step(spec, dataset, adam_cfg):
    # The gradient must point at a real descent direction (sanity for Adam).
    model = MLPModule(spec, dtype=np.float64, seed=11)
    pred0 = model.forward(dataset["x"])
    loss0, grad_out = mse_loss_and_grad(pred0, dataset["y"])
    grads = model.backward(dataset["x"], grad_out)
    new = {n: p - 0.05 * grads[n] for n, p in model.snapshot().items()}
    model.install(new)
    loss1, _ = mse_loss_and_grad(model.forward(dataset["x"]), dataset["y"])
    assert loss1 < loss0


def test_layout_gradients_align_with_params_by_identity(spec, dataset):
    """Grad[i] flattened with layout must correspond to param[i]'s slice."""

    model = MLPModule(spec, dtype=np.float64, seed=3)
    pred = model.forward(dataset["x"])
    _, grad_out = mse_loss_and_grad(pred, dataset["y"])
    grads = model.backward(dataset["x"], grad_out)
    layout = ParamLayout(tuple(spec.parameter_ids()))
    g_flat = layout.flatten(grads)
    p_flat = layout.flatten(model.snapshot())
    assert g_flat.shape == p_flat.shape
    # Reversing dict order must not move any gradient off its parameter slot.
    g_flat_rev = layout.flatten(dict(list(grads.items())[::-1]))
    np.testing.assert_array_equal(g_flat, g_flat_rev)
