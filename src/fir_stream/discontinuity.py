"""边界不连续(突跳)检测工具。

流式处理的输出由若干块拼接而成。若分块状态管理有误(例如块边界
处延迟线丢失、切换包络在块间推进错误),输出会在块边界处出现
阶跃式不连续。本模块提供数值检测:

- 一阶差分 d[n] = y[n] - y[n-1];
- 边界突跳 = 边界位置处的 |d| 显著超过信号自身的正常变化水平。

阈值策略:显式给定 threshold,或自动取
    max( 全局 |d| 的中位数 * factor , abs_floor )
中位数对少量跳变点鲁棒(跳变本身不会抬高阈值)。
对平滑的交叉渐变输出,边界处差分不会突出,因此不会被误报。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class DiscontinuityReport:
    n_samples: int
    n_boundaries: int
    threshold: float
    boundary_max_abs_diff: float
    global_max_abs_diff: float
    detected_boundaries: list[int] = field(default_factory=list)
    detected_values: list[float] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.detected_boundaries) == 0

    def to_dict(self) -> dict:
        return {
            "n_samples": self.n_samples,
            "n_boundaries": self.n_boundaries,
            "threshold": self.threshold,
            "boundary_max_abs_diff": self.boundary_max_abs_diff,
            "global_max_abs_diff": self.global_max_abs_diff,
            "detected_boundaries": self.detected_boundaries,
            "detected_values": self.detected_values,
            "ok": self.ok,
        }


def _channel_jumps(y: np.ndarray, boundaries: list[int], threshold: float):
    d = np.abs(np.diff(y))
    hits, vals = [], []
    for b in boundaries:
        if 1 <= b <= y.size - 1 and d[b - 1] > threshold:
            hits.append(b)
            vals.append(float(d[b - 1]))
    bmax = max((float(d[b - 1]) for b in boundaries if 1 <= b <= y.size - 1),
               default=0.0)
    gmax = float(d.max()) if d.size else 0.0
    return hits, vals, bmax, gmax


def detect_boundary_jumps(
    y: np.ndarray,
    boundaries,
    threshold: float | None = None,
    factor: float = 8.0,
    abs_floor: float = 1e-9,
) -> DiscontinuityReport:
    """检测块边界处的突跳。

    参数
    ----
    y          : 拼接后的输出,(n,) 或 (n, C)。
    boundaries : 块边界位置(样本索引,即每块起始位置,不含 0)。
    threshold  : 显式阈值;None 时自动估计。
    factor     : 自动阈值 = 全局 |diff| 中位数 * factor。
    abs_floor  : 自动阈值的下限(避免对近零信号过敏感)。
    """
    arr = np.asarray(y, dtype=np.float64)
    one_d = arr.ndim == 1
    arr2 = arr.reshape(-1, 1) if one_d else arr
    bounds = sorted({int(b) for b in boundaries if int(b) > 0})

    if threshold is None:
        all_d = np.abs(np.diff(arr2, axis=0))
        med = float(np.median(all_d)) if all_d.size else 0.0
        threshold = max(med * factor, abs_floor)

    hits, vals, bmax, gmax = [], [], 0.0, 0.0
    for ch in range(arr2.shape[1]):
        h_, v_, b_, g_ = _channel_jumps(arr2[:, ch], bounds, threshold)
        hits.extend(h_)
        vals.extend(v_)
        bmax = max(bmax, b_)
        gmax = max(gmax, g_)

    order = np.argsort(hits) if hits else []
    hits = [hits[i] for i in order]
    vals = [vals[i] for i in order]
    return DiscontinuityReport(
        n_samples=int(arr2.shape[0]),
        n_boundaries=len(bounds),
        threshold=float(threshold),
        boundary_max_abs_diff=bmax,
        global_max_abs_diff=gmax,
        detected_boundaries=hits,
        detected_values=vals,
    )


def max_abs_diff_at(y: np.ndarray, positions) -> float:
    """给定位置处 |y[n]-y[n-1]| 的最大值(诊断用)。"""
    arr = np.asarray(y, dtype=np.float64)
    arr2 = arr.reshape(-1, 1) if arr.ndim == 1 else arr
    d = np.abs(np.diff(arr2, axis=0))
    vals = [float(d[p - 1].max()) for p in positions if 1 <= p <= arr2.shape[0] - 1]
    return max(vals, default=0.0)
