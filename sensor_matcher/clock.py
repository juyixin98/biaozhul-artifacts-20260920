"""时钟偏移校正。

场景：两类传感器各自使用本地时钟，B 钟相对 A 钟存在近似恒定偏移
``t_true = t_b_raw + offset``，即
``t_a_raw - t_b_raw ≈ offset``。

校正方式：在配对前用一小段“已知同步对”（合成数据里取同时刻生成的轨迹点）
估计偏移（加权/裁剪中位数，对离群点稳健），随后在统一时基上做容差配对。
"""

from dataclasses import dataclass

import numpy as np

_EPS_NS = 1e-9  # 小于该值的残差视为 0，避免平局比较中的浮点噪声


@dataclass(frozen=True)
class ClockCorrection:
    """不可变时钟校正结果。

    Attributes:
        offset: 估计偏移，满足 t_corrected_b = raw_b + offset。
        n_used: 实际用于估计的同步点对数（裁剪后）。
        residuals_std: 所用同步点残差标准差，表征偏移稳定程度。
    """

    offset: float
    n_used: int
    residuals_std: float


def estimate_offset(
    pairs_a: np.ndarray,
    pairs_b: np.ndarray,
    trim_ratio: float = 0.1,
) -> ClockCorrection:
    """由同步点对估计 B 钟相对 A 钟的偏移。

    Args:
        pairs_a: 形状 (n,) 的 A 侧原始时间戳。
        pairs_b: 形状 (n,) 的 B 侧原始时间戳，与 ``pairs_a`` 一一对应，
            每一对对应物理上同一事件（合成数据中同时刻生成）。
        trim_ratio: 双侧裁剪比例，丢弃残差最大的若干离群对后再取中位数。
            取 0 时退化为普通中位数。

    Returns:
        ClockCorrection。残差定义为 ``raw_a - raw_b``，即校正量
        ``t_b_corrected = raw_b + offset``。
    """
    a = np.asarray(pairs_a, dtype=float).reshape(-1)
    b = np.asarray(pairs_b, dtype=float).reshape(-1)
    if a.shape != b.shape:
        raise ValueError(f"同步点长度不一致: {a.shape} vs {b.shape}")
    if a.size == 0:
        raise ValueError("至少需要一个同步点对来估计时钟偏移")
    if not 0.0 <= trim_ratio < 0.5:
        raise ValueError("trim_ratio 必须位于 [0, 0.5)")

    residuals = a - b
    if a.size == 1 or trim_ratio == 0.0:
        used = residuals
    else:
        k_trim = int(np.floor(trim_ratio * a.size))
        if 2 * k_trim >= a.size:
            k_trim = 0
        order = np.argsort(residuals)
        if k_trim > 0:
            order = order[k_trim:-k_trim]
        used = residuals[order]

    offset = float(np.median(used))
    std = float(np.std(used)) if used.size > 1 else 0.0
    return ClockCorrection(offset=offset, n_used=int(used.size), residuals_std=std)


def corrected_time(raw: float, offset: float) -> float:
    """施加偏移校正。offset≈0 时原样返回，避免无谓的浮点扰动。"""
    if abs(offset) < _EPS_NS:
        return float(raw)
    return float(raw) + offset
