"""Minimal multinomial logistic regression implemented with NumPy.

Purpose: prove the split output feeds a real (if simple) local ML training
loop -- no external models, no downloads. Full-batch gradient descent with
stable softmax, L2 regularization and deterministic initialization.
"""
from __future__ import annotations

import numpy as np


class LogisticRegression:
    def __init__(
        self,
        *,
        lr: float = 0.5,
        n_epochs: int = 300,
        l2: float = 1e-4,
        seed: int = 0,
    ) -> None:
        self.lr = lr
        self.n_epochs = n_epochs
        self.l2 = l2
        self.seed = seed
        self.W: np.ndarray | None = None  # (n_features, n_classes)
        self.b: np.ndarray | None = None  # (n_classes,)
        self.loss_history: list[float] = []

    def fit(self, X: np.ndarray, y: np.ndarray) -> "LogisticRegression":
        X = np.asarray(X, dtype=np.float64)
        y = np.asarray(y, dtype=np.int64)
        n, d = X.shape
        classes = np.unique(y)
        k = int(classes.max()) + 1
        # Standardize using training statistics (deterministic).
        self._mean = X.mean(axis=0)
        std = X.std(axis=0)
        self._std = np.where(std > 1e-12, std, 1.0)
        Xs = (X - self._mean) / self._std

        rng = np.random.default_rng(self.seed)
        self.W = rng.normal(0.0, 0.01, size=(d, k))
        self.b = np.zeros(k)
        Y = np.eye(k, dtype=np.float64)[y]

        self.loss_history = []
        for _ in range(self.n_epochs):
            logits = Xs @ self.W + self.b
            probs = _softmax(logits)
            loss = -(Y * np.log(probs + 1e-12)).sum() / n
            loss += 0.5 * self.l2 * float((self.W**2).sum())
            self.loss_history.append(float(loss))

            grad_logits = (probs - Y) / n
            grad_W = Xs.T @ grad_logits + self.l2 * self.W
            grad_b = grad_logits.sum(axis=0)
            self.W -= self.lr * grad_W
            self.b -= self.lr * grad_b
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if self.W is None or self.b is None:
            raise RuntimeError("model is not fitted")
        Xs = (np.asarray(X, dtype=np.float64) - self._mean) / self._std
        return _softmax(Xs @ self.W + self.b)

    def predict(self, X: np.ndarray) -> np.ndarray:
        return np.argmax(self.predict_proba(X), axis=1)

    def accuracy(self, X: np.ndarray, y: np.ndarray) -> float:
        return float(np.mean(self.predict(X) == np.asarray(y)))


def _softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - logits.max(axis=1, keepdims=True)
    exp = np.exp(shifted)
    return exp / exp.sum(axis=1, keepdims=True)
