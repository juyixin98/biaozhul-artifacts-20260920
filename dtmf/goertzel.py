"""Goertzel 频率检测库。

Goertzel 算法等价于在任意归一化频率上计算单点 DFT，复杂度 O(N)，
适合只关心少数几个目标频率的场景（如双音多频的 8 个频点）。

提供两种接口：
- goertzel_power       : 标量递归形式，教科书式实现，便于核对正确性；
- goertzel_power_batch : 向量化形式，对多帧 × 多频点一次性求功率，
                         数学上与递归形式完全等价（同为单点 DFT 的模平方）。
"""

from __future__ import annotations

import math

import numpy as np


def goertzel_power(samples: np.ndarray, freq: float, sample_rate: float) -> float:
    """递归 Goertzel：返回 samples 在 freq 处的功率（单点 DFT 模平方）。

    对频率 f，有 power = |sum_n x[n] * exp(-j*2*pi*f*n/fs)|^2。
    """
    w = 2.0 * math.pi * freq / sample_rate
    coeff = 2.0 * math.cos(w)
    s_prev = 0.0
    s_prev2 = 0.0
    for x in samples:
        s = float(x) + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    return s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2


def goertzel_power_batch(
    frames: np.ndarray, freqs: np.ndarray | list[float], sample_rate: float
) -> np.ndarray:
    """向量化 Goertzel：对多帧信号在多个频点上求功率。

    参数
    ----
    frames : (n_frames, n_samples) 或 (n_samples,) 数组
    freqs  : 目标频率列表（Hz）
    sample_rate : 采样率（Hz）

    返回
    ----
    (n_frames, n_freqs) 功率矩阵；输入为一维时返回 (n_freqs,)。
    """
    x = np.asarray(frames, dtype=np.float64)
    squeeze = x.ndim == 1
    if squeeze:
        x = x[np.newaxis, :]
    n = x.shape[1]
    freqs = np.asarray(freqs, dtype=np.float64)
    t = np.arange(n, dtype=np.float64)
    # E[m, k] = exp(-j * 2*pi * f_k * m / fs)
    kernel = np.exp(-2j * np.pi * np.outer(t, freqs) / sample_rate)
    power = np.abs(x @ kernel) ** 2
    return power[0] if squeeze else power


def estimate_amplitude(power: float, n_samples: int, window_sum: float | None = None) -> float:
    """由 Goertzel 功率反推正弦幅度。

    未加窗时 A = 2*|X|/N；加窗时用窗的相干增益（sum(w)）代替 N。
    """
    denom = window_sum if window_sum is not None else n_samples
    return 2.0 * math.sqrt(max(power, 0.0)) / denom
