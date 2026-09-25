"""抗混叠 FIR 设计：Kaiser 窗低通。

滤波器设计在上采样域（速率 L*fs_in）进行：
- 奈奎斯特约束截止 f_nyq = 0.5 * min(1/L, 1/M)（cycles/sample，对上采样速率归一化）；
- 过渡带 = transition_ratio * f_nyq，sinc 中心截止取过渡带中点；
- 直流增益归一化为 L，补偿零插值带来的幅度缩减。
"""

from __future__ import annotations

import numpy as np

DEFAULT_ATTENUATION_DB = 80.0
DEFAULT_TRANSITION_RATIO = 0.2


def kaiser_beta(attenuation_db: float) -> float:
    """Kaiser 窗形状参数（Kaiser 经验公式）。"""
    a = float(attenuation_db)
    if a > 50.0:
        return 0.1102 * (a - 8.7)
    if a >= 21.0:
        return 0.5842 * (a - 21.0) ** 0.4 + 0.07886 * (a - 21.0)
    return 0.0


def kaiser_length(attenuation_db: float, transition_width: float) -> int:
    """估计满足阻带衰减与过渡带要求的滤波器长度（返回奇数）。

    transition_width 以 cycles/sample 计（对上采样速率归一化）。
    """
    if transition_width <= 0:
        raise ValueError("transition_width must be positive")
    n = (attenuation_db - 8.0) / (2.285 * 2.0 * np.pi * transition_width) + 1.0
    n = int(np.ceil(n))
    if n % 2 == 0:
        n += 1
    return max(n, 3)


def design_anti_alias_fir(
    up: int,
    down: int,
    attenuation_db: float = DEFAULT_ATTENUATION_DB,
    transition_ratio: float = DEFAULT_TRANSITION_RATIO,
) -> np.ndarray:
    """设计 L/M 有理比重采样用的抗混叠/抗镜像低通 FIR。

    返回奇数长对称系数数组，直流增益为 ``up``。
    """
    if up < 1 or down < 1:
        raise ValueError("up and down must be positive integers")
    if attenuation_db < 21.0:
        raise ValueError("attenuation_db must be >= 21 dB")
    if not 0.0 < transition_ratio < 1.0:
        raise ValueError("transition_ratio must be in (0, 1)")

    f_nyq = 0.5 * min(1.0 / up, 1.0 / down)
    transition = transition_ratio * f_nyq
    cutoff = f_nyq - 0.5 * transition  # 过渡带中点（-6 dB 点）

    n = kaiser_length(attenuation_db, transition)
    beta = kaiser_beta(attenuation_db)

    t = np.arange(n, dtype=np.float64) - (n - 1) / 2.0
    h = 2.0 * cutoff * np.sinc(2.0 * cutoff * t) * np.kaiser(n, beta)
    h *= up / h.sum()  # 直流增益 = L，补偿零插值
    return h
