"""Computation graph: a small MLP with order-independent, stably named parameters.

Parameters live in a ``dict`` keyed by qualified names (``layers.0.weight`` …),
never in a positional list, so reordering parameters at restore time cannot
confuse identities. Forward/backward are closed-form (softmax cross-entropy +
ReLU layers), giving exact reference gradients independent of Adam.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .tensor_types import ParamId


def _he_init(rng: np.random.Generator, fan_in: int, fan_out: int) -> np.ndarray:
    scale = np.sqrt(2.0 / fan_in)
    return rng.standard_normal((fan_out, fan_in)) * scale


@dataclass(frozen=True)
class GraphSpec:
    """Structural description of a network: layer widths and parameter names."""

    dims: tuple[int, ...]

    def __post_init__(self) -> None:
        dims = tuple(int(d) for d in self.dims)
        if len(dims) < 2 or any(d <= 0 for d in dims):
            raise ValueError(f"need at least two positive dims, got {dims}")
        object.__setattr__(self, "dims", dims)

    @property
    def param_names(self) -> list[str]:
        names: list[str] = []
        for i in range(len(self.dims) - 1):
            names.append(f"layers.{i}.weight")
            names.append(f"layers.{i}.bias")
        return names

    def shape_of(self, name: str) -> tuple[int, ...]:
        idx = int(name.split(".")[1])
        if name.endswith(".weight"):
            return (self.dims[idx + 1], self.dims[idx])
        if name.endswith(".bias"):
            return (self.dims[idx + 1],)
        raise KeyError(name)

    def param_ids(self) -> list[ParamId]:
        return [ParamId(n, self.shape_of(n)) for n in self.param_names]


class MLPModel:
    """Mutable parameter holder with exact forward/backward for an ReLU MLP."""

    def __init__(self, spec: GraphSpec, seed: int) -> None:
        self.spec = spec
        rng = np.random.default_rng(seed)
        self.params: dict[str, np.ndarray] = {}
        for i in range(len(spec.dims) - 1):
            fan_in, fan_out = spec.dims[i], spec.dims[i + 1]
            self.params[f"layers.{i}.weight"] = _he_init(rng, fan_in, fan_out)
            self.params[f"layers.{i}.bias"] = np.zeros(fan_out)

    # -- identity / shape -------------------------------------------------
    def param_ids(self) -> list[ParamId]:
        return self.spec.param_ids()

    def named_params(self) -> dict[str, np.ndarray]:
        return dict(self.params)

    def set_params(self, values: dict[str, np.ndarray]) -> None:
        missing = set(self.params) - set(values)
        unknown = set(values) - set(self.params)
        if missing or unknown:
            raise KeyError(f"parameter set mismatch; missing={sorted(missing)} unknown={sorted(unknown)}")
        for name in self.spec.param_names:
            arr = np.asarray(values[name], dtype=np.float64)
            if arr.shape != self.params[name].shape:
                raise ValueError(f"shape mismatch setting {name}")
            self.params[name] = arr.copy()

    def clone(self) -> "MLPModel":
        m = MLPModel.__new__(MLPModel)
        m.spec = self.spec
        m.params = {k: v.copy() for k, v in self.params.items()}
        return m

    # -- forward / backward ----------------------------------------------
    def forward(self, x: np.ndarray) -> tuple[np.ndarray, dict]:
        activations: list[np.ndarray] = [x]
        pre_activations: list[np.ndarray] = []
        h = x
        n_layers = len(self.spec.dims) - 1
        for i in range(n_layers):
            w = self.params[f"layers.{i}.weight"]
            b = self.params[f"layers.{i}.bias"]
            z = h @ w.T + b
            pre_activations.append(z)
            if i < n_layers - 1:
                h = np.maximum(z, 0.0)
                activations.append(h)
        logits = z
        return logits, {"acts": activations, "pre": pre_activations}

    def backward(self, x: np.ndarray, y: np.ndarray, cache: dict) -> dict[str, np.ndarray]:
        """Exact gradients of mean softmax-CE w.r.t. every parameter."""
        acts = cache["acts"]
        pre = cache["pre"]
        batch = x.shape[0]
        logits = pre[-1]
        probs = _softmax(logits)
        probs[np.arange(batch), y] -= 1.0
        delta = probs / batch  # gradient w.r.t. final pre-activations

        grads: dict[str, np.ndarray] = {}
        n_layers = len(self.spec.dims) - 1
        for i in range(n_layers - 1, -1, -1):
            a_in = acts[i]
            grads[f"layers.{i}.weight"] = delta.T @ a_in
            grads[f"layers.{i}.bias"] = delta.sum(axis=0)
            if i > 0:
                w = self.params[f"layers.{i}.weight"]
                d_a = delta @ w
                delta = d_a * (pre[i - 1] > 0)
        return grads

    def loss(self, x: np.ndarray, y: np.ndarray) -> float:
        logits, _ = self.forward(x)
        probs = _softmax(logits)
        return float(-np.mean(np.log(probs[np.arange(x.shape[0]), y] + 1e-300)))


def _softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - logits.max(axis=1, keepdims=True)
    exp = np.exp(shifted)
    return exp / exp.sum(axis=1, keepdims=True)


def synthetic_dataset(
    n_samples: int, in_dim: int, n_classes: int, seed: int
) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic, locally generated fixture data (no external resources)."""
    rng = np.random.default_rng(seed)
    # Each class gets a random centroid; samples = centroid + noise => learnable.
    centroids = rng.standard_normal((n_classes, in_dim)) * 3.0
    y = rng.integers(0, n_classes, size=n_samples)
    x = centroids[y] + rng.standard_normal((n_samples, in_dim)) * 0.5
    return x.astype(np.float64), y.astype(np.int64)
