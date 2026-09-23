"""抗混叠 FIR 低通滤波器设计：Kaiser 窗截断 sinc。

只依赖 NumPy。频率单位为 周期/样本（cycles/sample），奈奎斯特为 0.5。
"""

from __future__ import annotations

import numpy as np


def kaiser_beta(atten_db: float) -> float:
    """由阻带衰减（dB）计算 Kaiser 窗 beta 参数（Kaiser 经验公式）。"""
    a = float(atten_db)
    if a > 50.0:
        return 0.1102 * (a - 8.7)
    if a >= 21.0:
        return 0.5842 * (a - 21.0) ** 0.4 + 0.07886 * (a - 21.0)
    return 0.0


def kaiser_num_taps(atten_db: float, transition_width: float) -> int:
    """由阻带衰减与过渡带宽度（cycles/sample）估计滤波器阶数。

    返回奇数阶数（保证群延迟为整数个样本）。
    """
    if transition_width <= 0.0:
        raise ValueError("transition_width 必须为正")
    n = (float(atten_db) - 8.0) / (2.285 * 2.0 * np.pi * transition_width) + 1.0
    n = int(np.ceil(n))
    if n % 2 == 0:
        n += 1
    return max(n, 3)


def design_lowpass(num_taps: int, cutoff: float, atten_db: float = 80.0) -> np.ndarray:
    """设计 Kaiser 窗低通 FIR，直流增益归一化为 1。

    参数
    ----
    num_taps : 抽头数（建议奇数，使群延迟为整数样本）
    cutoff   : 截止频率，cycles/sample，范围 (0, 0.5)
    atten_db : 期望阻带衰减，决定 Kaiser beta
    """
    num_taps = int(num_taps)
    if num_taps < 3:
        raise ValueError("num_taps 必须 >= 3")
    if not 0.0 < cutoff < 0.5:
        raise ValueError("cutoff 必须在 (0, 0.5) 内")
    n = np.arange(num_taps)
    gd = (num_taps - 1) / 2.0
    m = n - gd
    h = 2.0 * cutoff * np.sinc(2.0 * cutoff * m)
    h *= np.kaiser(num_taps, kaiser_beta(atten_db))
    h /= h.sum()
    return h


def frequency_response(h: np.ndarray, freqs: np.ndarray) -> np.ndarray:
    """在指定频率（cycles/sample）处计算 FIR 的复频率响应。"""
    n = np.arange(len(h))
    return np.exp(-2j * np.pi * np.outer(freqs, n)) @ h
