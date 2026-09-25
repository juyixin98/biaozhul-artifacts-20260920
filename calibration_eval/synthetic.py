"""可复现的合成数据与简单模型,用于验证校准评估机制。

不下载任何外部模型或数据:
- 用固定种子的 NumPy RNG 生成特征与标签;
- 用纯 NumPy 实现的逻辑回归(梯度下降)作为"简单模型";
- 通过温度缩放构造一个刻意失准的对照模型,展示 ECE 的区分能力。
"""

from __future__ import annotations

import numpy as np

SEED = 20260922


def sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-z))


def make_synthetic(
    n_samples: int = 2000,
    n_features: int = 5,
    seed: int = SEED,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """生成 (X, y, p_true):特征、由真实概率采样的标签、真实概率本身。

    p_true = sigmoid(X @ beta),y ~ Bernoulli(p_true)。
    p_true 是"完美校准"的参照:用它当预测概率时 ECE 应接近 0。
    """
    rng = np.random.default_rng(seed)
    X = rng.normal(0.0, 1.0, size=(n_samples, n_features))
    beta = rng.normal(0.0, 1.0, size=n_features)
    p_true = sigmoid(X @ beta)
    y = (rng.random(n_samples) < p_true).astype(np.float64)
    return X, y, p_true


def train_logistic_regression(
    X: np.ndarray,
    y: np.ndarray,
    lr: float = 0.5,
    n_iter: int = 500,
) -> tuple[np.ndarray, float]:
    """纯 NumPy 逻辑回归(批量梯度下降),返回 (权重, 偏置)。"""
    n, d = X.shape
    w = np.zeros(d)
    b = 0.0
    for _ in range(n_iter):
        p = sigmoid(X @ w + b)
        err = p - y
        w -= lr * (X.T @ err) / n
        b -= lr * float(np.sum(err)) / n
    return w, b


def temperature_scale(p: np.ndarray, temperature: float) -> np.ndarray:
    """温度缩放:对 logit 除以 T 再回 sigmoid。T>1 使概率向 0.5 收缩(失准)。"""
    eps = 1e-15
    p = np.clip(p, eps, 1.0 - eps)
    logit = np.log(p / (1.0 - p))
    return sigmoid(logit / temperature)
