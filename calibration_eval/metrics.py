"""校准指标定义与实现。

统一定义(均为加权平均,权重总和为 W = sum(w)):

- Brier 分数:  BS  = (1/W) * Σ w_i (p_i - y_i)^2,越小越好,完美预测为 0。
- 对数损失:    LL  = -(1/W) * Σ w_i [ y_i ln p_i + (1 - y_i) ln(1 - p_i) ]。
- ECE(期望校准误差):
  把 [0, 1] 等宽划分为 n_bins 个箱子,第 b 箱内
      acc(b)  = 箱内加权正例比例,  conf(b) = 箱内加权平均预测概率
      ECE = Σ_b (W_b / W) * |acc(b) - conf(b)|
  空箱跳过(权重为 0,不参与求和)。

概率端点策略(endpoint_strategy)只影响对数损失中 p=0 / p=1 的处理:
- "clip"(默认): 把概率裁剪到 [epsilon, 1 - epsilon],保证损失有限;
- "allow": 不裁剪,若预测与标签在端点冲突,对数损失为 +inf。
区间外的概率(如 -0.1、1.2、NaN)无论哪种策略都在校验阶段直接拒绝。
"""

from __future__ import annotations

import math

import numpy as np

from calibration_eval.validation import validate_inputs

DEFAULT_N_BINS = 10
DEFAULT_EPSILON = 1e-15
VALID_ENDPOINT_STRATEGIES = ("clip", "allow")


def _weighted_mean(values: np.ndarray, weights: np.ndarray) -> float:
    return float(np.dot(weights, values) / np.sum(weights))


def brier_score(
    y_true: object,
    y_prob: object,
    sample_weight: object | None = None,
) -> float:
    """加权 Brier 分数,(1/W) * Σ w_i (p_i - y_i)^2。"""
    y, p, w = validate_inputs(y_true, y_prob, sample_weight)
    return _weighted_mean((p - y) ** 2, w)


def log_loss(
    y_true: object,
    y_prob: object,
    sample_weight: object | None = None,
    endpoint_strategy: str = "clip",
    epsilon: float = DEFAULT_EPSILON,
) -> float:
    """加权对数损失(交叉熵)。

    endpoint_strategy:
    - "clip": 概率裁剪到 [epsilon, 1 - epsilon];
    - "allow": 原样使用,端点冲突时结果为 +inf。
    """
    if endpoint_strategy not in VALID_ENDPOINT_STRATEGIES:
        raise ValueError(
            f"endpoint_strategy 必须是 {VALID_ENDPOINT_STRATEGIES} 之一, "
            f"实际为 {endpoint_strategy!r}"
        )
    if not (0.0 < epsilon < 0.5):
        raise ValueError(f"epsilon 必须在 (0, 0.5) 内, 实际为 {epsilon}")

    y, p, w = validate_inputs(y_true, y_prob, sample_weight)
    if endpoint_strategy == "clip":
        p = np.clip(p, epsilon, 1.0 - epsilon)

    with np.errstate(divide="ignore", invalid="ignore"):
        per_sample = -(y * np.log(p) + (1.0 - y) * np.log1p(-p))
    # allow 策略下 0 * log(0) 会产生 NaN,按约定 0 * log(0) = 0 处理;
    # 真正冲突的项(p=0 且 y=1)保持为 +inf。
    per_sample = np.where(np.isnan(per_sample), 0.0, per_sample)
    return _weighted_mean(per_sample, w)


def _bin_index(p: np.ndarray, n_bins: int) -> np.ndarray:
    """等宽分箱索引:[0, 1/n), [1/n, 2/n), ..., 最后一箱含右端点 1.0。"""
    idx = np.floor(p * n_bins).astype(np.int64)
    return np.clip(idx, 0, n_bins - 1)


def calibration_bins(
    y_true: object,
    y_prob: object,
    sample_weight: object | None = None,
    n_bins: int = DEFAULT_N_BINS,
) -> list[dict]:
    """逐箱统计,返回每个箱子的加权样本量、平均置信度、实际正例率与校准差。

    空箱也会返回(weight=0,其余字段为 None),便于调用方画可靠性图。
    """
    if not isinstance(n_bins, int) or isinstance(n_bins, bool) or n_bins < 1:
        raise ValueError(f"n_bins 必须是正整数, 实际为 {n_bins!r}")

    y, p, w = validate_inputs(y_true, y_prob, sample_weight)
    idx = _bin_index(p, n_bins)

    bins: list[dict] = []
    for b in range(n_bins):
        mask = idx == b
        w_b = float(np.sum(w[mask]))
        lower = b / n_bins
        upper = (b + 1) / n_bins
        if w_b > 0.0:
            conf = float(np.dot(w[mask], p[mask]) / w_b)
            acc = float(np.dot(w[mask], y[mask]) / w_b)
            count = int(np.count_nonzero(w[mask] > 0.0))
            gap = abs(acc - conf)
        else:
            conf = acc = gap = None
            count = 0
        bins.append(
            {
                "bin": b,
                "lower": lower,
                "upper": upper,
                "count": count,
                "weight": w_b,
                "confidence": conf,
                "accuracy": acc,
                "gap": gap,
            }
        )
    return bins


def expected_calibration_error(
    y_true: object,
    y_prob: object,
    sample_weight: object | None = None,
    n_bins: int = DEFAULT_N_BINS,
) -> float:
    """期望校准误差:Σ_b (W_b / W) * |acc(b) - conf(b)|。"""
    bins = calibration_bins(y_true, y_prob, sample_weight, n_bins)
    total_weight = sum(b["weight"] for b in bins)
    # total_weight 必大于 0(validate_inputs 已保证)
    return float(
        sum((b["weight"] / total_weight) * b["gap"] for b in bins if b["weight"] > 0.0)
    )


def evaluate(
    y_true: object,
    y_prob: object,
    sample_weight: object | None = None,
    n_bins: int = DEFAULT_N_BINS,
    endpoint_strategy: str = "clip",
    epsilon: float = DEFAULT_EPSILON,
) -> dict:
    """一次性计算全部指标,返回可 JSON 序列化的字典。"""
    brier = brier_score(y_true, y_prob, sample_weight)
    ll = log_loss(y_true, y_prob, sample_weight, endpoint_strategy, epsilon)
    bins = calibration_bins(y_true, y_prob, sample_weight, n_bins)
    total_weight = sum(b["weight"] for b in bins)
    ece = float(
        sum((b["weight"] / total_weight) * b["gap"] for b in bins if b["weight"] > 0.0)
    )
    return {
        "n_samples": int(sum(b["count"] for b in bins)),
        "total_weight": total_weight,
        "n_bins": n_bins,
        "endpoint_strategy": endpoint_strategy,
        "brier_score": brier,
        "log_loss": ll if math.isfinite(ll) else "inf",
        "ece": ece,
        "bins": bins,
    }
