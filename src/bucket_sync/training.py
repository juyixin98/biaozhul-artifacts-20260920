"""Training state and local gradient computation.

This module owns the *model*: the current parameter values and how a
single worker computes gradients on its local batch.  The teaching model
is a linear regressor ``y = x @ w + b`` with mean-squared-error loss,
which keeps the math transparent and exactly reproducible by the
independent reference oracle in :mod:`bucket_sync.reference`.

It knows nothing about buckets, rounds, or other workers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

import numpy as np

from bucket_sync.graph import ParameterGraph

#: canonical parameter names for the teaching model
WEIGHT_PARAM = "w"
BIAS_PARAM = "b"


class TrainingError(ValueError):
    """Raised when a local training computation is given invalid input."""


def build_linear_graph(in_features: int, *, name: str = "linear-mse") -> ParameterGraph:
    """Parameter graph for the teaching model: weight matrix + bias vector.

    Both parameters are trainable; tests may rebuild a graph with a frozen
    parameter via :class:`ParameterNode` to exercise placeholder handling.
    """
    from bucket_sync.graph import ParameterNode  # local import to keep top clean
    from bucket_sync.tensor_types import TensorSpec

    return ParameterGraph(
        name,
        [
            ParameterNode(TensorSpec(WEIGHT_PARAM, (in_features, 1))),
            ParameterNode(TensorSpec(BIAS_PARAM, (1,))),
        ],
    )


@dataclass
class ModelState:
    """The trainable state: parameter values plus the graph they belong to.

    ``step`` counts committed optimizer updates; it is bumped by the
    coordinator on commit, never inside a round.
    """

    graph: ParameterGraph
    params: Dict[str, np.ndarray]
    step: int = 0

    def __post_init__(self) -> None:
        for node in self.graph:
            if node.spec.name not in self.params:
                raise TrainingError(
                    f"ModelState missing value for parameter {node.spec.name!r}"
                )
            node.spec.validate_value(self.params[node.spec.name])

    def copy_with(self, new_params: Dict[str, np.ndarray]) -> "ModelState":
        """Return a new state with updated params (immutable update pattern)."""
        merged = {k: np.array(v, copy=True) for k, v in self.params.items()}
        merged.update({k: np.array(v, copy=True) for k, v in new_params.items()})
        return ModelState(graph=self.graph, params=merged, step=self.step + 1)


def linear_mse_gradients(
    params: Dict[str, np.ndarray],
    x: np.ndarray,
    y: np.ndarray,
) -> Tuple[Dict[str, np.ndarray], int]:
    """Gradients of mean-squared error for ``y_hat = x @ w + b``.

    Returns ``(grads, n_samples)``.  The gradients are the *sum* over the
    batch divided by 1 (i.e. batch sums), **not** the batch mean: the
    reducer divides by the true global sample count, so each worker must
    report sums together with its own ``n_samples``.  Dividing here by the
    local batch size would silently mis-weight unequal batches.
    """
    w = np.asarray(params[WEIGHT_PARAM], dtype=np.float64)
    b = np.asarray(params[BIAS_PARAM], dtype=np.float64)
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)

    if x.ndim != 2:
        raise TrainingError(f"x must be 2-D (n, features), got shape {x.shape}")
    n = x.shape[0]
    if n == 0:
        raise TrainingError("empty batch: cannot compute gradients over 0 samples")
    if y.shape != (n, 1):
        raise TrainingError(f"y must have shape ({n}, 1), got {y.shape}")
    if w.shape[0] != x.shape[1]:
        raise TrainingError(
            f"w has {w.shape[0]} rows but x has {x.shape[1]} features"
        )
    for name, arr in (("x", x), ("y", y), ("w", w), ("b", b)):
        if not np.all(np.isfinite(arr)):
            raise TrainingError(f"{name} contains NaN or Inf")

    residual = x @ w + b - y  # (n, 1)
    grad_w = 2.0 * (x.T @ residual)  # sum over batch of d(loss)/dw
    grad_b = 2.0 * np.sum(residual, axis=0, keepdims=False).reshape(1)
    return {WEIGHT_PARAM: grad_w, BIAS_PARAM: grad_b}, n
