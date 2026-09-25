"""极简逻辑回归（纯 NumPy）与训练/评估工具，用于验证 PIT join 的影响。

不引入任何 ML 框架；批量梯度下降 + 标准化，足以比较
"防泄漏特征" 与 "朴素 as-of（含事后修订）特征" 的离线/在线差距。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Standardizer:
    mean: np.ndarray
    scale: np.ndarray

    @classmethod
    def fit(cls, x: np.ndarray) -> "Standardizer":
        mean = x.mean(axis=0)
        scale = x.std(axis=0)
        scale = np.where(scale < 1e-12, 1.0, scale)
        return cls(mean=mean, scale=scale)

    def transform(self, x: np.ndarray) -> np.ndarray:
        return (x - self.mean) / self.scale


@dataclass(frozen=True)
class LogisticRegression:
    """带 L2 的逻辑回归；``w`` 已在标准化空间中拟合。"""

    standardizer: Standardizer
    w: np.ndarray
    b: float
    n_iter: int
    train_loss: float

    @staticmethod
    def _sigmoid(z: np.ndarray) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-z))

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        z = self.standardizer.transform(np.asarray(x, dtype=np.float64)) @ self.w + self.b
        return self._sigmoid(z)

    def predict(self, x: np.ndarray, threshold: float = 0.5) -> np.ndarray:
        return (self.predict_proba(x) >= threshold).astype(np.int64)


def _loss(x: np.ndarray, y: np.ndarray, w: np.ndarray, b: float, l2: float) -> float:
    logits = x @ w + b
    # logaddexp 保证数值稳定
    loss = np.logaddexp(0.0, logits) - y * logits
    return float(loss.mean() + 0.5 * l2 * float(w @ w))


def train_logistic(
    x: np.ndarray,
    y: np.ndarray,
    *,
    l2: float = 1e-3,
    lr: float = 0.1,
    n_iter: int = 2000,
    tol: float = 1e-8,
) -> LogisticRegression:
    """全批量梯度下降训练。"""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    if x.ndim != 2:
        raise ValueError(f"x 必须是二维矩阵，实际 ndim={x.ndim}")
    if x.shape[0] != y.shape[0]:
        raise ValueError("x 与 y 样本数不一致")

    standardizer = Standardizer.fit(x)
    xs = standardizer.transform(x)
    n, d = xs.shape
    w = np.zeros(d, dtype=np.float64)
    b = 0.0
    prev_loss = float("inf")

    for step in range(n_iter):
        probs = LogisticRegression._sigmoid(xs @ w + b)
        grad = probs - y
        w -= lr * (xs.T @ grad / n + l2 * w)
        b -= lr * float(grad.mean())
        loss = _loss(xs, y, w, b, l2)
        if abs(prev_loss - loss) < tol:
            n_iter = step + 1
            break
        prev_loss = loss

    return LogisticRegression(
        standardizer=standardizer, w=w, b=b, n_iter=n_iter, train_loss=loss
    )


def accuracy(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float((np.asarray(y_true) == np.asarray(y_pred)).mean())


@dataclass(frozen=True)
class Split:
    x_train: np.ndarray
    x_test: np.ndarray
    y_train: np.ndarray
    y_test: np.ndarray


def chronological_split(frame, train_ratio: float = 0.7) -> Split:
    """按事件时刻做时间序列切分（绝不打乱，避免用未来训练过去）。"""
    order = np.argsort(frame.event_times, kind="stable")
    n_train = int(len(order) * train_ratio)
    train_idx, test_idx = order[:n_train], order[n_train:]
    x = frame.design_matrix()
    y = frame.labels.astype(np.int64)
    return Split(
        x_train=x[train_idx],
        x_test=x[test_idx],
        y_train=y[train_idx],
        y_test=y[test_idx],
    )
