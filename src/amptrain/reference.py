"""Independent numerical validation oracle.

The oracle is a *separate* full-precision (fp64) implementation of the same
MLP and SGD-momentum dynamics.  Test reference answers must NOT be generated
by the code under test, so nothing here imports the trainer's math: it
recomputes forward/backward from the network definition directly.

The expected mixed-precision behaviour is derived analytically relative to
this oracle:

* on committed steps the trainer's fp32 master weights follow the same
  update sequence (within a tolerance accounting for fp16 forward error);
* on skipped steps the master weights are *bit-identical* before/after;
* LR-schedule progress depends only on committed steps.
"""

from __future__ import annotations

import numpy as np

from .config import RunConfig
from .tensors import PARAM_NAMES


def _act(z: np.ndarray, kind: str) -> np.ndarray:
    return np.maximum(z, 0.0) if kind == "relu" else np.tanh(z)


def _act_grad(z: np.ndarray, a: np.ndarray, kind: str) -> np.ndarray:
    return (z > 0).astype(np.float64) if kind == "relu" else 1.0 - a * a


class FullPrecisionOracle:
    """fp64 re-implementation, driven by the same finite input batches."""

    def __init__(self, config: RunConfig, initial_master: dict[str, np.ndarray]):
        self.cfg = config
        self.w = {name: initial_master[name].astype(np.float64).copy() for name in PARAM_NAMES}
        self.velocity = {name: np.zeros_like(self.w[name]) for name in PARAM_NAMES}
        self.committed_steps = 0

    def gradients(self, batches: list[tuple[np.ndarray, np.ndarray]]) -> dict[str, np.ndarray]:
        """Mean fp64 gradient over the window, *unscaled* (scale = 1)."""
        acc = {name: np.zeros_like(self.w[name]) for name in PARAM_NAMES}
        for x_raw, y_raw in batches:
            x = x_raw.astype(np.float64)
            t = y_raw.astype(np.float64)
            z1 = x @ self.w["W1"]
            a1 = _act(z1, self.cfg.model.activation)
            y = a1 @ self.w["W2"]
            n = x.shape[0]
            d_y = 2.0 * (y - t) / n
            g2 = a1.T @ d_y
            d_z1 = (d_y @ self.w["W2"].T) * _act_grad(
                z1, a1, self.cfg.model.activation
            )
            g1 = x.T @ d_z1
            acc["W1"] += g1
            acc["W2"] += g2
        return {name: acc[name] / len(batches) for name in PARAM_NAMES}

    def expected_lr(self) -> float:
        if self.cfg.optimizer.schedule == "constant":
            return self.cfg.optimizer.lr
        drops = self.committed_steps // self.cfg.optimizer.step_size
        return self.cfg.optimizer.lr * (self.cfg.optimizer.gamma ** drops)

    def step(self, batches: list[tuple[np.ndarray, np.ndarray]]) -> dict[str, np.ndarray]:
        """Apply one expected committed step using the scheduled LR."""
        grads = self.gradients(batches)
        lr = self.expected_lr()
        for name in PARAM_NAMES:
            if self.cfg.optimizer.weight_decay > 0.0:
                grads[name] = grads[name] + self.cfg.optimizer.weight_decay * self.w[name]
            self.velocity[name] = (
                self.cfg.optimizer.momentum * self.velocity[name] + grads[name]
            )
            self.w[name] = self.w[name] - lr * self.velocity[name]
        self.committed_steps += 1
        return {name: self.w[name].copy() for name in PARAM_NAMES}

    def loss(self, x: np.ndarray, y: np.ndarray) -> float:
        x = x.astype(np.float64)
        z1 = x @ self.w["W1"]
        a1 = _act(z1, self.cfg.model.activation)
        pred = a1 @ self.w["W2"]
        return float(np.mean((pred - y.astype(np.float64)) ** 2))


def analytical_overflow(
    x: np.ndarray, scale: float, lowp_dtype: np.dtype
) -> bool:
    """Independent overflow predicate: does amplification * scale plausibly
    exceed the low-precision range for an O(1) network?

    A conservative, model-free bound on the first matmul: with entries
    ~amplification and weights initialized with scale ~1/sqrt(fan_in),
    hidden pre-activations have magnitude roughly
    ``amplification * sqrt(fan_in)``; squaring that in the loss and
    multiplying by the loss scale must stay below the dtype max.  This is a
    test-design aid, not part of the trainer -- the trainer detects overflow
    by actually computing in low precision.
    """
    amp = float(np.max(np.abs(x)))
    bound = (amp * amp) * scale
    return not np.isfinite(np.array(bound, dtype=lowp_dtype)) or bound > float(
        np.finfo(lowp_dtype).max
    ) * 0.5
