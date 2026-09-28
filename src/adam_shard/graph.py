"""A small but genuine computation graph: MLP with tanh hidden layers.

Forward and analytic backward are implemented from first principles -- no
autodiff backend -- so gradients feeding Adam are real, loss-dependent
quantities rather than canned fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .tensor_types import TensorId


def _weight_name(layer: int) -> str:
    return f"layers.{layer}.weight"


def _bias_name(layer: int) -> str:
    return f"layers.{layer}.bias"


@dataclass(frozen=True)
class GraphSpec:
    """Architecture description; parameter names are position independent."""

    dims: tuple[int, ...]
    name: str = "mlp-tanh"

    def __post_init__(self) -> None:
        if len(self.dims) < 2 or any(d <= 0 for d in self.dims):
            raise ValueError(f"dims must be >=2 positive ints, got {self.dims}")

    @property
    def num_layers(self) -> int:
        return len(self.dims) - 1

    def parameter_ids(self) -> list[TensorId]:
        ids: list[TensorId] = []
        for layer in range(self.num_layers):
            ids.append(TensorId(_weight_name(layer), (self.dims[layer + 1], self.dims[layer])))
            ids.append(TensorId(_bias_name(layer), (self.dims[layer + 1],)))
        return ids

    @classmethod
    def from_config(cls, cfg: dict) -> "GraphSpec":
        return cls(dims=tuple(int(d) for d in cfg["dims"]), name=str(cfg.get("name", "mlp-tanh")))


def _tanh(x: np.ndarray) -> np.ndarray:
    return np.tanh(x)


class MLPModule:
    """Trainable MLP.  Parameters are kept in a name-keyed, order-independent dict."""

    def __init__(self, spec: GraphSpec, dtype: np.dtype, seed: int) -> None:
        self.spec = spec
        self.dtype = np.dtype(dtype)
        rng = np.random.default_rng(seed)
        self.params: dict[str, np.ndarray] = {}
        # Named, independently seeded initial values (orthogonal-stable scaling).
        for layer in range(spec.num_layers):
            fan_in, fan_out = spec.dims[layer], spec.dims[layer + 1]
            bound = np.sqrt(6.0 / (fan_in + fan_out))
            w = rng.uniform(-bound, bound, size=(fan_out, fan_in)).astype(self.dtype)
            b = np.zeros(fan_out, dtype=self.dtype)
            self.params[_weight_name(layer)] = w
            self.params[_bias_name(layer)] = b
        self._validate()

    def _validate(self) -> None:
        have = {TensorId(n, a.shape) for n, a in self.params.items()}
        want = set(self.spec.parameter_ids())
        if have != want:
            missing = sorted(str(i) for i in want - have)
            extra = sorted(str(i) for i in have - want)
            raise ValueError(f"parameter set mismatch, missing={missing} extra={extra}")

    # ------------------------------------------------------------------ forward
    def forward(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=self.dtype)
        if x.ndim != 2 or x.shape[1] != self.spec.dims[0]:
            raise ValueError(f"bad input shape {x.shape}, want (N, {self.spec.dims[0]})")
        h = x
        for layer in range(self.spec.num_layers):
            w = self.params[_weight_name(layer)]
            b = self.params[_bias_name(layer)]
            z = h @ w.T + b
            h = z if layer == self.spec.num_layers - 1 else _tanh(z)
        return h

    # ------------------------------------------------------------------ backward
    def backward(self, x: np.ndarray, grad_out: np.ndarray) -> dict[str, np.ndarray]:
        """Analytic reverse-mode gradients for MSE's externally supplied upstream.

        Returns a name-keyed dict aligned with ``self.params``.
        """

        x = np.asarray(x, dtype=self.dtype)
        grad_out = np.asarray(grad_out, dtype=self.dtype)

        # Re-cache activations and pre-activations.
        acts = [x]
        pres: list[np.ndarray | None] = []
        h = x
        for layer in range(self.spec.num_layers):
            z = h @ self.params[_weight_name(layer)].T + self.params[_bias_name(layer)]
            pres.append(z)
            h = z if layer == self.spec.num_layers - 1 else _tanh(z)
            acts.append(h)

        grads: dict[str, np.ndarray] = {}
        # ``grad_out`` already includes the loss normalization (mean over N*D),
        # so no further per-batch division belongs here.
        delta = grad_out
        for layer in reversed(range(self.spec.num_layers)):
            w = self.params[_weight_name(layer)]
            grads[_weight_name(layer)] = delta.T @ acts[layer]
            grads[_bias_name(layer)] = delta.sum(axis=0)
            if layer > 0:
                # tanh derivative at the pre-activation of the previous layer.
                prev_z = pres[layer - 1]
                assert prev_z is not None
                d_act = 1.0 - np.tanh(prev_z) ** 2
                delta = (delta @ w) * d_act
        return grads

    # ------------------------------------------------------------- manipulation
    def snapshot(self) -> dict[str, np.ndarray]:
        return {n: a.copy() for n, a in self.params.items()}

    def install(self, params: dict[str, np.ndarray]) -> None:
        """Validate-by-identity then install a new parameter set (immutable swap)."""

        new_params = {n: np.asarray(a, dtype=self.dtype) for n, a in params.items()}
        probe = MLPModule.__new__(MLPModule)
        probe.spec = self.spec
        probe.dtype = self.dtype
        probe.params = new_params
        probe._validate()
        self.params = new_params


def mse_loss_and_grad(pred: np.ndarray, target: np.ndarray) -> tuple[float, np.ndarray]:
    """Mean-squared error plus its gradient w.r.t. predictions."""

    if pred.shape != target.shape:
        raise ValueError(f"shape mismatch: pred {pred.shape} vs target {target.shape}")
    diff = pred - target
    loss = float(np.mean(diff**2))
    grad = 2.0 * diff / diff.size
    return loss, grad
