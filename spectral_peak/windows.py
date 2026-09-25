"""窗函数及其相干增益。

只依赖 NumPy。相干增益用于把 FFT 幅度换算回正弦峰值幅度。
"""

from __future__ import annotations

import numpy as np

SUPPORTED_WINDOWS = ("hann", "rectangular", "blackmanharris")


def get_window(name: str, n: int) -> np.ndarray:
    """返回长度为 n 的窗函数（周期型，与 FFT 分析匹配）。"""
    if n <= 0:
        raise ValueError(f"窗长必须为正整数，得到 {n}")
    if name == "hann":
        # np.hanning 是对称型（端点为 0），周期型更贴合频谱分析
        return np.hanning(n + 1)[:-1] if n > 1 else np.ones(1)
    if name == "rectangular":
        return np.ones(n)
    if name == "blackmanharris":
        k = np.arange(n)
        return (
            0.35875
            - 0.48829 * np.cos(2.0 * np.pi * k / n)
            + 0.14128 * np.cos(4.0 * np.pi * k / n)
            - 0.01168 * np.cos(6.0 * np.pi * k / n)
        )
    raise ValueError(f"不支持的窗: {name!r}，可选 {SUPPORTED_WINDOWS}")


def coherent_gain(window: np.ndarray) -> float:
    """窗的相干增益（均值）。正弦峰值幅度 = 2*|X[k]| / sum(w)。"""
    return float(np.mean(window))
