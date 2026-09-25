"""Tiny logistic regression in NumPy - the 'simple model' for end-to-end checks.

It exists only to demonstrate that the drift monitor slots into a real
training/serving loop: train on the baseline window, then watch both
feature drift and a model-performance proxy (ROC-AUC) on the current
window. Missing values are mean-imputed; features are standardized with
frozen baseline statistics.
"""
from __future__ import annotations

import numpy as np


def roc_auc(y_true: np.ndarray, score: np.ndarray) -> float:
    """ROC-AUC via the Mann-Whitney rank statistic (no ties handling needed
    beyond rank averaging). Returns NaN if one class is absent."""
    y = np.asarray(y_true, dtype=np.float64).reshape(-1)
    s = np.asarray(score, dtype=np.float64).reshape(-1)
    if y.size != s.size or y.size == 0:
        raise ValueError("y_true and score must be non-empty and equal length")
    n_pos = float(np.sum(y == 1))
    n_neg = float(np.sum(y == 0))
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(y.size, dtype=np.float64)
    s_sorted = s[order]
    # Average ranks for tied scores: sorted positions i..j-1 carry the
    # mean of (1-indexed) ranks i+1 .. j, i.e. (i + 1 + j) / 2.
    i = 0
    while i < y.size:
        j = i + 1
        while j < y.size and s_sorted[j] == s_sorted[i]:
            j += 1
        ranks[order[i:j]] = (i + 1 + j) / 2.0
        i = j
    return float((np.sum(ranks[y == 1]) - n_pos * (n_pos + 1) / 2.0)
                 / (n_pos * n_neg))


class LogisticModel:
    """Mean-impute + z-score + logistic regression trained by full-batch GD."""

    def __init__(self, feature_names: list[str], lr: float = 0.1,
                 n_steps: int = 2000, l2: float = 1e-4) -> None:
        self.feature_names = list(feature_names)
        self.lr = lr
        self.n_steps = n_steps
        self.l2 = l2
        self.means_: np.ndarray | None = None
        self.scales_: np.ndarray | None = None
        self.w_: np.ndarray | None = None
        self.b_: float = 0.0

    def _matrix(self, data: dict[str, np.ndarray],
                fit_stats: bool) -> np.ndarray:
        X = np.column_stack([
            np.asarray(data[name], dtype=np.float64).reshape(-1)
            for name in self.feature_names
        ])
        if fit_stats:
            self.means_ = np.nanmean(X, axis=0)
            std = np.nanstd(X, axis=0)
            self.scales_ = np.where(std > 1e-12, std, 1.0)
        assert self.means_ is not None and self.scales_ is not None
        inds = np.where(np.isnan(X))
        X[inds] = np.take(self.means_, inds[1])
        return (X - self.means_) / self.scales_

    def fit(self, data: dict[str, np.ndarray], y: np.ndarray) -> "LogisticModel":
        X = self._matrix(data, fit_stats=True)
        y = np.asarray(y, dtype=np.float64).reshape(-1)
        rng = np.random.default_rng(0)
        self.w_ = rng.normal(scale=0.01, size=X.shape[1])
        self.b_ = 0.0
        n = X.shape[0]
        for _ in range(self.n_steps):
            z = X @ self.w_ + self.b_
            p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
            err = p - y
            grad_w = X.T @ err / n + self.l2 * self.w_
            grad_b = float(np.mean(err))
            self.w_ -= self.lr * grad_w
            self.b_ -= self.lr * grad_b
        return self

    def predict_proba(self, data: dict[str, np.ndarray]) -> np.ndarray:
        if self.w_ is None:
            raise RuntimeError("model is not fitted")
        X = self._matrix(data, fit_stats=False)
        z = X @ self.w_ + self.b_
        return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
