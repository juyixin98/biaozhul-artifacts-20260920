"""亚频点（sub-bin）插值估计器。

三种三点插值方法：

- ``hann-exact``：针对 Hann 窗主瓣形状 |W(x)| ∝ |sin(pi x) / (x (1 - x^2))|
  推导的精确三点公式（大 N 近似下无系统偏差）：
      delta = 2*(g - a) / (2*b + a + g)
- ``log-parabolic``：对数幅度抛物线插值，对高斯窗精确，对 Hann 窗近似良好。
- ``quinn``：Quinn 第二估计器，使用复数频点，适合矩形窗。

所有方法都假设峰是**孤立单峰**（主瓣内无其他分量），否则结果有偏，
是否受邻峰影响由 detector 中的 interference 标志提示。
"""

from __future__ import annotations

import numpy as np

SUPPORTED_METHODS = ("auto", "hann-exact", "log-parabolic", "quinn")

# 防止 log(0)
_TINY = 1e-300


def resolve_method(method: str, window: str) -> str:
    """把 ``auto`` 解析为具体方法。"""
    if method == "auto":
        return "hann-exact" if window == "hann" else "log-parabolic"
    if method not in SUPPORTED_METHODS:
        raise ValueError(f"不支持的插值方法: {method!r}，可选 {SUPPORTED_METHODS}")
    return method


def hann_exact_delta(mag: np.ndarray, k: int) -> float:
    """Hann 窗精确三点估计，返回相对 bin k 的小数偏移（约 [-1, 1]）。"""
    a, b, g = mag[k - 1], mag[k], mag[k + 1]
    denom = 2.0 * b + a + g
    if denom <= 0.0:
        return 0.0
    delta = 2.0 * (g - a) / denom
    return float(np.clip(delta, -1.0, 1.0))


def log_parabolic_delta(mag: np.ndarray, k: int) -> float:
    """对数幅度抛物线三点估计，返回 [-0.5, 0.5] 内的小数偏移。"""
    a = np.log(max(mag[k - 1], _TINY))
    b = np.log(max(mag[k], _TINY))
    g = np.log(max(mag[k + 1], _TINY))
    denom = a - 2.0 * b + g
    if denom >= 0.0:
        # 峰处二阶差分必须为负，否则三点不构成抛物线峰
        return 0.0
    delta = 0.5 * (a - g) / denom
    return float(np.clip(delta, -0.5, 0.5))


def quinn_delta(X: np.ndarray, k: int) -> float:
    """Quinn 第二估计器（复频点），返回小数偏移。"""
    denom = X[k]
    if denom == 0:
        return 0.0
    ap = (X[k + 1] / denom).real
    am = (X[k - 1] / denom).real
    dp = -ap / (1.0 - ap) if ap != 1.0 else 0.0
    dm = am / (1.0 - am) if am != 1.0 else 0.0

    def tau(x: float) -> float:
        return 0.25 * np.log(x * x + 2.0 * x + 1.0)

    delta = 0.5 * (dp + dm) + tau(dp * dp) - tau(dm * dm)
    return float(np.clip(delta, -1.0, 1.0))


def estimate_delta(method: str, X: np.ndarray, mag: np.ndarray, k: int) -> float:
    """按方法计算亚频点偏移。调用方保证 1 <= k <= len(mag) - 2。"""
    if method == "hann-exact":
        return hann_exact_delta(mag, k)
    if method == "log-parabolic":
        return log_parabolic_delta(mag, k)
    if method == "quinn":
        return quinn_delta(X, k)
    raise ValueError(f"不支持的插值方法: {method!r}")


def hann_amplitude_correction(delta: float) -> float:
    """Hann 窗主瓣在偏移 delta 处的衰减补偿系数（>= 1）。

    |W(0)| / |W(delta)| = pi*delta*(1 - delta^2) / sin(pi*delta)，delta->0 时为 1。
    """
    d = abs(float(delta))
    if d < 1e-12:
        return 1.0
    if d >= 1.0:
        d = 1.0 - 1e-12
    return float(np.pi * d * (1.0 - d * d) / np.sin(np.pi * d))


def log_parabolic_peak_mag(mag: np.ndarray, k: int, delta: float) -> float:
    """对数抛物线顶点处的幅度（线性量纲）。"""
    a = np.log(max(mag[k - 1], _TINY))
    b = np.log(max(mag[k], _TINY))
    g = np.log(max(mag[k + 1], _TINY))
    peak_log = b - 0.25 * (a - g) * delta
    return float(np.exp(peak_log))
