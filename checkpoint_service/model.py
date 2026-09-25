"""Tiny linear regression model: y = X @ W + b.

Implemented from scratch with NumPy (no ML framework). Parameters are
float32; gradients are computed analytically.
"""

from __future__ import annotations

import numpy as np


class LinearModel:
    def __init__(self, n_features: int, seed: int) -> None:
        # Dedicated RNG for initialization; the trainer owns the RNG that
        # drives shuffling, but weights are drawn once at run creation.
        init_rng = np.random.default_rng(seed)
        self.W = (init_rng.standard_normal(n_features) * 0.01).astype(np.float32)
        self.b = np.float32(0.0)

    @classmethod
    def from_params(cls, W: np.ndarray, b: np.ndarray) -> "LinearModel":
        obj = object.__new__(cls)
        obj.W = np.asarray(W, dtype=np.float32).copy()
        obj.b = np.float32(b)
        return obj

    def predict(self, X: np.ndarray) -> np.ndarray:
        return X @ self.W + self.b

    def loss_and_grad(
        self, X: np.ndarray, y: np.ndarray, l2: float
    ) -> tuple[float, np.ndarray, np.ndarray]:
        """Mean-squared-error loss (means over the batch) plus L2 on W.

        Returns
        -------
        loss, dW, db
            Python-float loss and float32 gradients.
        """
        batch = X.shape[0]
        pred = self.predict(X)
        residual = pred - y  # (batch,)
        loss = float(np.mean(residual * residual) + l2 * float(np.dot(self.W, self.W)))

        # d MSE / d pred = 2/batch * residual
        scale = np.float32(2.0 / batch)
        dW = (X * residual[:, None]).sum(axis=0) * scale + np.float32(2.0 * l2) * self.W
        db = residual.sum() * scale
        return loss, dW.astype(np.float32), np.float32(db)
