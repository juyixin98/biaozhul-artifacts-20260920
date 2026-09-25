"""可复现的合成数据（纯 NumPy，不下载任何外部数据或模型）。

提供两类场景：

- :func:`same_distribution`：基线/当前同分布（N(0,1)），期望指标接近 0；
- :func:`shifted_distribution`：当前窗口均值平移，期望 PSI 明显增大；
- 另有尺度变化、缺值注入、极端值注入等辅助生成器，供测试与演示组合。

所有函数接受 ``seed``，相同入参必得相同数据。
"""
from __future__ import annotations

import numpy as np

# 默认合成场景参数（命名常量，避免魔法数字）
DEFAULT_BASELINE_N = 2000
DEFAULT_CURRENT_N = 500
DEFAULT_MEAN_SHIFT = 1.0


def _rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def same_distribution(
    n_baseline: int = DEFAULT_BASELINE_N,
    n_current: int = DEFAULT_CURRENT_N,
    seed: int = 42,
) -> tuple[np.ndarray, np.ndarray]:
    """同分布场景：两窗口均为标准正态 N(0, 1)。"""
    rng = _rng(seed)
    baseline = rng.standard_normal(n_baseline)
    current = rng.standard_normal(n_current)
    return baseline, current


def shifted_distribution(
    n_baseline: int = DEFAULT_BASELINE_N,
    n_current: int = DEFAULT_CURRENT_N,
    mean_shift: float = DEFAULT_MEAN_SHIFT,
    seed: int = 42,
) -> tuple[np.ndarray, np.ndarray]:
    """平移场景：基线 N(0,1)，当前 N(mean_shift, 1)。"""
    rng = _rng(seed)
    baseline = rng.standard_normal(n_baseline)
    current = rng.standard_normal(n_current) + mean_shift
    return baseline, current


def scaled_distribution(
    n_baseline: int = DEFAULT_BASELINE_N,
    n_current: int = DEFAULT_CURRENT_N,
    scale: float = 2.0,
    seed: int = 42,
) -> tuple[np.ndarray, np.ndarray]:
    """尺度变化场景：基线 N(0,1)，当前 N(0, scale^2)。"""
    rng = _rng(seed)
    baseline = rng.standard_normal(n_baseline)
    current = rng.standard_normal(n_current) * scale
    return baseline, current


def inject_missing(
    values: np.ndarray,
    rate: float,
    seed: int = 0,
) -> np.ndarray:
    """按比例把元素替换成 NaN（返回新数组，不修改入参）。"""
    if not 0.0 <= rate <= 1.0:
        raise ValueError("缺失率必须在 [0, 1]")
    out = np.array(values, dtype=float, copy=True)
    rng = _rng(seed)
    mask = rng.random(out.shape[0]) < rate
    out[mask] = np.nan
    return out


def inject_extremes(
    values: np.ndarray,
    rate: float = 0.02,
    magnitude: float = 100.0,
    seed: int = 0,
) -> np.ndarray:
    """注入远超基线范围的极端值（落溢出桶），返回新数组。"""
    if not 0.0 <= rate <= 1.0:
        raise ValueError("极端值比例必须在 [0, 1]")
    out = np.array(values, dtype=float, copy=True)
    rng = _rng(seed)
    mask = rng.random(out.shape[0]) < rate
    signs = rng.choice([-1.0, 1.0], size=mask.sum())
    out[mask] = signs * magnitude
    return out


def small_sample(n: int = 10, seed: int = 7) -> tuple[np.ndarray, np.ndarray]:
    """小样本同分布场景（默认每窗口 10 个观测）。"""
    return same_distribution(n_baseline=n, n_current=n, seed=seed)


def all_missing_current(
    n_baseline: int = DEFAULT_BASELINE_N,
    n_current: int = DEFAULT_CURRENT_N,
    seed: int = 42,
) -> tuple[np.ndarray, np.ndarray]:
    """当前窗口全部缺失的退化场景。"""
    baseline, _ = same_distribution(n_baseline, 1, seed)
    current = np.full(n_current, np.nan)
    return baseline, current
