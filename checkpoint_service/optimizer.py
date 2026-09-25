"""Mini-batch SGD with Nesterov-free classical momentum.

The velocity buffer is optimizer *state*: a checkpoint that stores weights
but not velocity cannot reproduce an uninterrupted run.
"""

from __future__ import annotations

import numpy as np


class MomentumSgd:
    def __init__(self, n_features: int, lr: float, momentum: float) -> None:
        self.lr = float(lr)
        self.momentum = float(momentum)
        self.vW = np.zeros(n_features, dtype=np.float32)
        self.vb = np.float32(0.0)

    @classmethod
    def from_state(
        cls, vW: np.ndarray, vb: np.ndarray, lr: float, momentum: float
    ) -> "MomentumSgd":
        obj = object.__new__(cls)
        obj.lr = float(lr)
        obj.momentum = float(momentum)
        obj.vW = np.asarray(vW, dtype=np.float32).copy()
        obj.vb = np.float32(vb)
        return obj

    def step(
        self,
        W: np.ndarray,
        b: np.ndarray,
        dW: np.ndarray,
        db: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        # v = mu * v + g ;  theta -= lr * v
        self.vW = np.float32(self.momentum) * self.vW + dW.astype(np.float32)
        self.vb = np.float32(self.momentum) * self.vb + np.float32(db)
        W_new = (W - np.float32(self.lr) * self.vW).astype(np.float32)
        b_new = np.float32(b - np.float32(self.lr) * self.vb)
        return W_new, b_new
