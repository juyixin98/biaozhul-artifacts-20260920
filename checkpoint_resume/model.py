"""小型线性模型与 MSE 损失。"""

from __future__ import annotations

import numpy as np


class LinearModel:
    """y = X @ w + b"""

    def __init__(self, n_features: int, rng: np.random.RandomState):
        self.w = rng.randn(n_features, 1) * 0.1
        self.b = np.zeros(1)

    def predict(self, X: np.ndarray) -> np.ndarray:
        return X @ self.w + self.b

    def params(self) -> dict:
        return {"w": self.w.copy(), "b": self.b.copy()}

    def state_dict(self) -> dict:
        return {"model_w": self.w.copy(), "model_b": self.b.copy()}

    def load_state_dict(self, state: dict) -> None:
        self.w = np.asarray(state["model_w"], dtype=np.float64).copy()
        self.b = np.asarray(state["model_b"], dtype=np.float64).copy()


def mse_loss_and_grads(model: LinearModel, Xb: np.ndarray, yb: np.ndarray):
    """返回 (loss, grad_w, grad_b)。"""
    n = Xb.shape[0]
    err = model.predict(Xb) - yb
    loss = float(np.mean(err * err))
    grad_w = (2.0 / n) * (Xb.T @ err)
    grad_b = (2.0 / n) * err.sum(axis=0)
    return loss, grad_w, grad_b
