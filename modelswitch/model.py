"""Deterministic linear-softmax model used to exercise the serving stack.

The model is intentionally simple: the point of this project is the
load/validate/warm-up/atomic-switch machinery around it, not model quality.
Weights are immutable after construction; `predict` is pure and thread-safe.
"""
from __future__ import annotations

import numpy as np


class LinearModel:
    """y = softmax(x @ W + b). Weights are treated as read-only."""

    def __init__(self, weights: np.ndarray, bias: np.ndarray) -> None:
        if weights.ndim != 2:
            raise ValueError(f"weights must be 2-D, got shape {weights.shape}")
        if bias.ndim != 1 or bias.shape[0] != weights.shape[1]:
            raise ValueError(
                f"bias must be 1-D of length {weights.shape[1]}, got shape {bias.shape}"
            )
        # Defensive copies so callers cannot mutate our parameters later.
        self._weights = np.array(weights, dtype=np.float64, copy=True)
        self._bias = np.array(bias, dtype=np.float64, copy=True)
        self._weights.setflags(write=False)
        self._bias.setflags(write=False)

    @property
    def input_dim(self) -> int:
        return int(self._weights.shape[0])

    @property
    def output_dim(self) -> int:
        return int(self._weights.shape[1])

    def predict(self, x: np.ndarray) -> np.ndarray:
        """Map input row(s) (..., input_dim) to probabilities (..., output_dim)."""
        x = np.asarray(x, dtype=np.float64)
        if x.ndim == 1:
            x = x.reshape(1, -1)
        if x.ndim != 2 or x.shape[1] != self.input_dim:
            raise ValueError(
                f"expected input of shape (n, {self.input_dim}), got {x.shape}"
            )
        # Non-finite intermediate results are possible with pathological
        # weights; they surface as non-finite outputs, which warm-up rejects.
        # No need to spam warnings about it on every call.
        with np.errstate(over="ignore", invalid="ignore"):
            logits = x @ self._weights + self._bias
            return _softmax(logits)


def _softmax(logits: np.ndarray) -> np.ndarray:
    """Numerically stable row-wise softmax."""
    shifted = logits - np.max(logits, axis=-1, keepdims=True)
    exp = np.exp(shifted)
    return exp / np.sum(exp, axis=-1, keepdims=True)
