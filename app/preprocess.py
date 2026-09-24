"""预处理：单位换算、坐标轴重映射、重复时间合并、时间缺口分段。

设计要点
--------
1. 单位与坐标轴在最开始完成，后续算法只处理 SI + XYZ 右手系。
2. **时间重复**（相同时间戳的多个样本，例如回放/双卡口）合并为均值；
   合并仅在各重复样本彼此一致（逐轴极差 <= dup_tol 个稳健标准差）时进行，
   不一致的重复（疑似异常）整体剔除并计入统计，绝不静默取一条。
3. 时间戳必须单调不减；出现回退直接报错（请求层错误，不猜测顺序）。
4. **时间缺口**（gap > factor * 中位间隔）切段，静止识别与零偏估计按段独立执行，
   避免跨越丢包段滑窗把运动/静止混在一起。
"""

from __future__ import annotations

import dataclasses

import numpy as np

from .config import RuntimeConfig
from .errors import PreprocessError


@dataclasses.dataclass
class Segment:
    index: int
    start: int  # 在合并后数组中的起止下标（含端点）
    end: int
    t_start: float
    t_end: float
    duration: float
    n_samples: int


@dataclasses.dataclass
class CleanData:
    time: np.ndarray               # (N,) 秒，单调递增
    accel: np.ndarray              # (N,3) m/s^2，XYZ
    gyro: np.ndarray               # (N,3) rad/s，XYZ
    temperature: np.ndarray | None  # (N,) 或 None
    segments: list[Segment]
    median_dt: float
    preprocess: dict               # 处理统计，原样进入响应


def _robust_consistent(group: np.ndarray, scale: np.ndarray, tol: float = 6.0) -> bool:
    """组内逐轴极差是否在 tol * 全局稳健尺度以内。"""
    ptp = np.max(group, axis=0) - np.min(group, axis=0)
    return bool(np.all(ptp <= tol * np.maximum(scale, 1e-12)))


def preprocess(
    timestamps: np.ndarray,
    accel: np.ndarray,
    gyro: np.ndarray,
    temperature: np.ndarray | None,
    cfg: RuntimeConfig,
) -> CleanData:
    n = len(timestamps)

    # 1) 单位换算
    t = np.asarray(timestamps, dtype=float) * cfg.time_scale
    a = np.asarray(accel, dtype=float) * cfg.accel_scale
    g = np.asarray(gyro, dtype=float) * cfg.gyro_scale
    temp = None if temperature is None else np.asarray(temperature, dtype=float)

    # 2) 坐标轴重映射（内部 XYZ <- 输入列，含变号）
    p = cfg.axis_perm
    s = np.asarray(cfg.axis_sign)
    a = a[:, p] * s
    g = g[:, p] * s

    if np.any(np.diff(t) < 0):
        first = int(np.argmax(np.diff(t) < 0))
        raise PreprocessError(
            "timestamps 必须单调不减；检测到时间回退，拒绝猜测重排",
            {"at_index": first, "t_prev": float(t[first]), "t_next": float(t[first + 1])},
        )

    # 3) 完全相同时间戳的重复样本：一致则均值合并，不一致则整组剔除
    mad_a = np.median(np.abs(a - np.median(a, axis=0)), axis=0)
    scale_a = 1.4826 * mad_a
    mad_g = np.median(np.abs(g - np.median(g, axis=0)), axis=0)
    scale_g = 1.4826 * mad_g

    keep_mask = np.ones(n, dtype=bool)
    n_duplicate_groups = 0
    n_duplicate_merged = 0
    n_duplicate_dropped = 0
    # 同值时间戳分组边界
    change = np.flatnonzero(np.diff(t)) + 1
    groups = np.split(np.arange(n), change)
    for grp in groups:
        if len(grp) <= 1:
            continue
        n_duplicate_groups += 1
        consistent = _robust_consistent(a[grp], scale_a) and _robust_consistent(
            g[grp], scale_g
        )
        if consistent:
            a[grp[0]] = np.mean(a[grp], axis=0)
            g[grp[0]] = np.mean(g[grp], axis=0)
            if temp is not None:
                temp[grp[0]] = np.mean(temp[grp])
            t[grp[0]] = t[grp[0]]
            keep_mask[grp[1:]] = False
            n_duplicate_merged += len(grp) - 1
        else:
            keep_mask[grp] = False
            n_duplicate_dropped += len(grp)

    t = t[keep_mask]
    a = a[keep_mask]
    g = g[keep_mask]
    if temp is not None:
        temp = temp[keep_mask]

    if len(t) == 0:
        raise PreprocessError("重复时间戳全部被判定为不一致，清洗后无有效样本")

    # 4) 时间缺口分段
    dts = np.diff(t)
    if len(dts) > 0:
        median_dt = float(np.median(dts[dts > 0])) if np.any(dts > 0) else 0.0
    else:
        median_dt = 0.0

    segments: list[Segment] = []
    if median_dt <= 0.0:
        bounds = [0, len(t) - 1]
    else:
        gap_idx = np.flatnonzero(dts > cfg.time_gap_factor * median_dt)
        bounds = [0]
        for gi in gap_idx:
            bounds.extend([int(gi), int(gi + 1)])
        bounds.append(len(t) - 1)

    n_gaps = (len(bounds) - 2) // 2 if median_dt > 0 else 0
    seg_id = 0
    for k in range(0, len(bounds), 2):
        s_i, e_i = bounds[k], bounds[k + 1]
        if e_i < s_i:
            continue
        duration = float(t[e_i] - t[s_i])
        segments.append(
            Segment(
                index=seg_id,
                start=s_i,
                end=e_i,
                t_start=float(t[s_i]),
                t_end=float(t[e_i]),
                duration=duration,
                n_samples=e_i - s_i + 1,
            )
        )
        seg_id += 1

    preprocess_stats = {
        "input_samples": n,
        "kept_samples": int(len(t)),
        "duplicate_timestamp_groups": n_duplicate_groups,
        "duplicate_samples_merged": n_duplicate_merged,
        "duplicate_samples_dropped_inconsistent": n_duplicate_dropped,
        "time_gaps": n_gaps,
        "segments": len(segments),
        "median_sample_interval_s": median_dt,
    }
    return CleanData(
        time=t,
        accel=a,
        gyro=g,
        temperature=temp,
        segments=segments,
        median_dt=median_dt,
        preprocess=preprocess_stats,
    )
