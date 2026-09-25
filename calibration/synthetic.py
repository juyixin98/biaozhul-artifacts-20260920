"""可复现的合成数据与简单模型。

不下载任何外部模型或数据：用固定种子的 NumPy 随机数生成逻辑回归
"真实概率"，再由伯努利抽样得到标签。另提供一个故意欠校准的简单
模型（温度缩放），用于演示评估流程。
"""

from __future__ import annotations

import numpy as np

from .errors import CalibrationError


def logistic_sigmoid(x: np.ndarray) -> np.ndarray:
    """数值稳定的 sigmoid。"""
    out = np.empty_like(x, dtype=np.float64)
    pos = x >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-x[pos]))
    exp_x = np.exp(x[~pos])
    out[~pos] = exp_x / (1.0 + exp_x)
    return out


def make_synthetic_labels(
    n_samples: int = 1000,
    *,
    seed: int = 42,
    prior: float = 0.3,
) -> dict:
    """生成合成的二分类数据集。

    数据生成过程（全部参数固定、可复现）：

    1. 单特征 ``X ~ N(0, 1)``；
    2. 真实对数几率 ``z = log(prior/(1-prior)) + 1.5*X``；
    3. 真实概率 ``p_true = sigmoid(z)``；
    4. 标签 ``y ~ Bernoulli(p_true)``。

    Parameters
    ----------
    prior:
        截距对应的基准正类比例，取极端值（如 0.01）可模拟类别极不均衡。
    """
    if not isinstance(n_samples, int) or n_samples <= 0:
        raise CalibrationError(
            "INVALID_INPUT", f"n_samples 必须是正整数，收到 {n_samples!r}"
        )
    if not 0.0 < prior < 1.0:
        raise CalibrationError(
            "INVALID_INPUT", f"prior 必须落在 (0, 1)，收到 {prior!r}"
        )

    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n_samples)
    intercept = np.log(prior / (1.0 - prior))
    p_true = logistic_sigmoid(intercept + 1.5 * x)
    y = (rng.random(n_samples) < p_true).astype(np.float64)
    return {"X": x, "y": y, "p_true": p_true}


def simple_model_proba(x: np.ndarray, *, temperature: float = 1.0) -> np.ndarray:
    """一个简单的线性概率模型。

    ``temperature > 1`` 让预测概率向 0.5 收缩（欠自信），
    ``temperature < 1`` 让概率更极端（过自信），便于制造校准误差。
    """
    if temperature <= 0:
        raise CalibrationError(
            "INVALID_INPUT", f"temperature 必须为正数，收到 {temperature!r}"
        )
    x = np.asarray(x, dtype=np.float64)
    z = 1.5 * x / temperature
    return logistic_sigmoid(z)


def make_demo_dataset(
    n_samples: int = 1000,
    *,
    seed: int = 42,
    prior: float = 0.3,
    temperature: float = 2.0,
) -> dict:
    """生成演示数据：真实概率 + 一个故意欠校准模型的预测概率。"""
    data = make_synthetic_labels(n_samples, seed=seed, prior=prior)
    proba = simple_model_proba(data["X"], temperature=temperature)
    data["proba"] = proba
    data["positive_prevalence"] = float(np.mean(data["y"]))
    return data
