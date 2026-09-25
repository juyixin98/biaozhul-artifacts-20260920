"""端到端便捷接口：原始数组 -> 分箱 -> 计数 -> 漂移指标。"""
from __future__ import annotations

from typing import Iterable, Mapping

import numpy as np

from .binning import FixedBins, assign_counts, fit_fixed_bins
from .metrics import (
    DEFAULT_ALPHA,
    DEFAULT_EPSILON,
    DEFAULT_MIN_SAMPLE,
    DriftResult,
    SmoothingMethod,
    compute_drift,
)


def monitor_feature(
    baseline_values: Iterable[float],
    current_values: Iterable[float],
    feature: str = "feature",
    n_bins: int = 10,
    bins: FixedBins | None = None,
    smoothing: SmoothingMethod = "laplace",
    alpha: float = DEFAULT_ALPHA,
    epsilon: float = DEFAULT_EPSILON,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> DriftResult:
    """单特征漂移监测。

    ``bins`` 为 None 时用基线数据拟合固定等宽桶；也可传入历史保存的
    :class:`FixedBins`，保证与更早的基线严格对齐。
    """
    fitted = bins if bins is not None else fit_fixed_bins(
        baseline_values, n_bins=n_bins
    )
    base_counts = assign_counts(fitted, baseline_values)
    cur_counts = assign_counts(fitted, current_values)
    return compute_drift(
        base_counts,
        cur_counts,
        feature=feature,
        smoothing=smoothing,
        alpha=alpha,
        epsilon=epsilon,
        min_sample=min_sample,
    )


def monitor_features(
    baseline: Mapping[str, Iterable[float]],
    current: Mapping[str, Iterable[float]],
    n_bins: int = 10,
    smoothing: SmoothingMethod = "laplace",
    alpha: float = DEFAULT_ALPHA,
    epsilon: float = DEFAULT_EPSILON,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> dict[str, DriftResult]:
    """多特征批处理；以基线特征集合为准，缺失的当前特征按空窗口处理。

    当前窗口里出现、基线里没有的特征会被跳过并在返回值之外不做推断——
    没有基线就无法定义漂移，调用方应自行决定如何处理新特征。
    """
    results: dict[str, DriftResult] = {}
    empty: np.ndarray = np.empty(0, dtype=float)
    for name, base_values in baseline.items():
        cur_values = current.get(name, empty)
        results[name] = monitor_feature(
            base_values,
            cur_values,
            feature=name,
            n_bins=n_bins,
            smoothing=smoothing,
            alpha=alpha,
            epsilon=epsilon,
            min_sample=min_sample,
        )
    return results
