"""Training state: parameters, gradients, versioned SGD updates.

This is a genuine (small) trainer rather than a hard-coded number: a linear
model ``y = X @ W + b`` is trained with mean-squared-error gradients computed
through the strided tensor ops.  Every parameter update bumps a monotonic
version and records where it happened, so request logs can correlate a state
change with the request that caused it.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..tensor import Tensor, ops
from ..tensor.errors import TensorError

_state_counter = itertools.count(1)


@dataclass(frozen=True)
class UpdateRecord:
    version: int
    location: str
    request_id: str | None
    losses: tuple[float, ...]
    learning_rate: float


@dataclass
class Parameter:
    name: str
    value: Tensor
    gradient: Tensor | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "shape": list(self.value.shape),
            "strides": list(self.value.strides),
            "storage_id": self.value.storage.storage_id,
            "version_storage_id": self.value.storage.storage_id,
        }


class TrainingState:
    """Holds trainable parameters and applies versioned optimizer steps."""

    def __init__(self, learning_rate: float = 0.1, *, state_id: str | None = None) -> None:
        self.state_id = state_id or f"train-state-{next(_state_counter)}"
        self.learning_rate = float(learning_rate)
        self._params: dict[str, Parameter] = {}
        self.version = 0
        self.history: list[UpdateRecord] = []

    # -------------------------------------------------------------- parameters

    def add_parameter(self, name: str, value: Tensor) -> None:
        if name in self._params:
            raise KeyError(f"parameter {name!r} already present")
        self._params[name] = Parameter(name=name, value=value)

    def get(self, name: str) -> Parameter:
        return self._params[name]

    def parameter_names(self) -> list[str]:
        return sorted(self._params)

    def init_linear(self, in_features: int, seed: int = 0) -> None:
        """Create ``W`` (in_features x 1) and ``b`` (1,) with a seeded RNG."""
        rng = np.random.default_rng(seed)
        self.add_parameter("W", Tensor.from_values(rng.normal(scale=0.1, size=(in_features, 1))))
        self.add_parameter("b", Tensor.from_values(np.zeros((1,))))

    # ---------------------------------------------------------------- training

    def predict(self, x: Tensor) -> Tensor:
        w = self._params["W"].value
        b = self._params["b"].value
        # Broadcast bias via a zero-stride view inside ops.binary.
        return ops.binary("add", ops.matmul(x, w), b.unsqueeze(0))

    def mse_gradients(self, x: Tensor, y: Tensor) -> tuple[Tensor, Tensor, float]:
        """Analytic MSE gradients for a linear model; returns (grad_W, grad_b, loss)."""
        n = x.shape[0]
        pred = self.predict(x)
        diff = ops.binary("subtract", pred, y)  # (n,1)
        loss = float(np.asarray(
            ops.reduce_sum(ops.unary("square", diff)).materialize()
        ).reshape(()) / n)
        # grad_W = 2/n * X^T @ diff  (transpose is a zero-copy view)
        xt = x.transpose(tuple(range(x.ndim))[::-1])
        grad_w = ops.scale(ops.matmul(xt, diff), 2.0 / n)
        grad_b = ops.scale(ops.reduce_sum(diff, axis=0), 2.0 / n)
        return grad_w, grad_b, loss

    def sgd_step(
        self,
        x: Tensor,
        y: Tensor,
        *,
        location: str = "sgd_step",
        request_id: str | None = None,
    ) -> float:
        """One gradient-descent step applied *in place* to W and b.

        The update ``p <- p - lr * grad`` writes into the parameter storage;
        parameter and gradient live in distinct storage, so the write is
        overlap-safe without a temporary.
        """
        if "W" not in self._params or "b" not in self._params:
            raise TensorError("training state has no linear parameters; call init_linear")
        grad_w, grad_b, loss = self.mse_gradients(x, y)
        w = self._params["W"].value
        b = self._params["b"].value
        scaled_w = ops.scale(grad_w, self.learning_rate)
        scaled_b = ops.scale(grad_b, self.learning_rate)
        ops.binary("subtract", w, scaled_w, out=w)
        ops.binary("subtract", b, scaled_b, out=b)
        self._params["W"].gradient = grad_w
        self._params["b"].gradient = grad_b
        self.version += 1
        self.history.append(UpdateRecord(
            version=self.version,
            location=location,
            request_id=request_id,
            losses=(loss,),
            learning_rate=self.learning_rate,
        ))
        return loss

    def train(self, x: Tensor, y: Tensor, steps: int, *, request_id: str | None = None) -> list[float]:
        return [
            self.sgd_step(x, y, location=f"train:{i + 1}/{steps}", request_id=request_id)
            for i in range(steps)
        ]

    # ------------------------------------------------------------------ export

    def snapshot(self) -> dict[str, Any]:
        return {
            "state_id": self.state_id,
            "version": self.version,
            "learning_rate": self.learning_rate,
            "parameters": [
                {
                    "name": p.name,
                    "shape": list(p.value.shape),
                    "strides": list(p.value.strides),
                    "values": p.value.to_list(),
                    "storage_id": p.value.storage.storage_id,
                }
                for p in self._params.values()
            ],
            "last_locations": [h.location for h in self.history[-5:]],
        }
