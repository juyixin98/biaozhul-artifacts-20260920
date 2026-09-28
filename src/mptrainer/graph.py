"""Compute-graph layer: a small MLP with manual forward/backward.

The graph is deliberately explicit (no autograd framework) so that the
precision of every intermediate is visible: activations and gradients live
in the low-precision dtype selected by the trainer, while the loss is also
evaluated in that dtype so fp16 overflow surfaces as inf/nan exactly the
way it would on real hardware.

Loss: mean squared error over the batch, ``mean((pred - y) ** 2)``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .tensors import ParamDict


@dataclass(frozen=True)
class LayerSpec:
    index: int
    n_in: int
    n_out: int


def layer_specs(layer_sizes: list[int]) -> list[LayerSpec]:
    """[in, h1, ..., out] -> one Linear spec per adjacent pair."""
    if len(layer_sizes) < 2:
        raise ValueError("layer_sizes needs at least input and output size")
    return [
        LayerSpec(index=i, n_in=layer_sizes[i], n_out=layer_sizes[i + 1])
        for i in range(len(layer_sizes) - 1)
    ]


def param_names(specs: list[LayerSpec]) -> list[str]:
    return [f"{kind}{s.index}" for s in specs for kind in ("W", "b")]


def init_params(layer_sizes: list[int], seed: int) -> ParamDict:
    """Deterministic fp32 init (He-scaled weights, zero biases)."""
    rng = np.random.default_rng(seed)
    params: ParamDict = {}
    for spec in layer_specs(layer_sizes):
        std = np.sqrt(2.0 / spec.n_in)
        params[f"W{spec.index}"] = (
            rng.standard_normal((spec.n_in, spec.n_out)) * std
        ).astype(np.float32)
        params[f"b{spec.index}"] = np.zeros(spec.n_out, dtype=np.float32)
    return params


@dataclass
class ForwardCache:
    """Intermediates needed by backward, all in the forward dtype."""

    activations: list[np.ndarray]      # a_0 = input ... a_L = network output
    pre_activations: list[np.ndarray]  # z_i before ReLU (hidden layers only)
    scaled_loss: float                 # loss * loss_scale, as a python float


def forward(
    params: ParamDict,
    x: np.ndarray,
    y: np.ndarray,
    loss_scale: float,
    dtype: np.dtype,
) -> ForwardCache:
    """Run the MLP in ``dtype``; return cache with the *scaled* loss.

    Hidden layers use ReLU; the output layer is linear. The loss is
    multiplied by ``loss_scale`` so that backprop yields scaled gradients.
    """
    a = x.astype(dtype)
    activations = [a]
    pre_activations: list[np.ndarray] = []
    n_layers = len(layer_specs_from_params(params))
    # Overflow to inf/nan is the intended detection signal for the
    # trainer's finite check, not an error -- silence numpy's warnings.
    with np.errstate(over="ignore", invalid="ignore"):
        target = y.astype(dtype)
        for i in range(n_layers):
            z = a @ params[f"W{i}"] + params[f"b{i}"]
            if i < n_layers - 1:
                pre_activations.append(z)
                a = np.maximum(z, dtype.type(0))
            else:
                a = z  # linear output
            activations.append(a)
        diff = a - target
        loss = np.mean(diff * diff)
        scaled = np.asarray(loss, dtype=dtype) * np.asarray(loss_scale, dtype=dtype)
    return ForwardCache(
        activations=activations,
        pre_activations=pre_activations,
        scaled_loss=float(np.asarray(scaled, dtype=np.float32)),
    )


def backward(
    params: ParamDict,
    cache: ForwardCache,
    y: np.ndarray,
    loss_scale: float,
) -> ParamDict:
    """Gradients of the *scaled* loss, in the forward dtype.

    d(scale * mean((p-y)^2))/dp = scale * 2(p-y)/N. If the forward pass
    overflowed, the inf/nan propagates into the gradients here and the
    trainer's finite check rejects the whole update.
    """
    dtype = cache.activations[-1].dtype
    n_layers = len(layer_specs_from_params(params))
    batch = cache.activations[-1].shape[0]
    grads: ParamDict = {}
    with np.errstate(over="ignore", invalid="ignore"):
        target = y.astype(dtype)
        diff = cache.activations[-1] - target
        grad = dtype.type(loss_scale) * (dtype.type(2.0) * diff / dtype.type(batch))
        for i in reversed(range(n_layers)):
            a_prev = cache.activations[i]
            grads[f"W{i}"] = a_prev.T @ grad
            grads[f"b{i}"] = grad.sum(axis=0)
            if i > 0:
                grad = grad @ params[f"W{i}"].T
                grad = grad * (cache.pre_activations[i - 1] > 0)
    return grads


def layer_specs_from_params(params: ParamDict) -> list[LayerSpec]:
    """Rebuild layer specs from a param dict (W0,b0,W1,b1,... ordering)."""
    indices = sorted(int(name[1:]) for name in params if name.startswith("W"))
    return [
        LayerSpec(index=i, n_in=params[f"W{i}"].shape[0], n_out=params[f"W{i}"].shape[1])
        for i in indices
    ]
