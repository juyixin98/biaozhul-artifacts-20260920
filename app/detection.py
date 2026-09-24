"""静止候选识别：滑窗方差 + 重力幅值准则，形态学闭运算合并区间。"""

from __future__ import annotations

import dataclasses

import numpy as np

from .config import RuntimeConfig
from .preprocess import CleanData, Segment


@dataclasses.dataclass
class Window:
    segment_index: int
    start: int          # 合并后数组下标，含
    end: int            # 含
    t0: float
    t1: float
    n: int
    gyro_med: np.ndarray   # (3,) rad/s
    gyro_std: np.ndarray   # (3,) rad/s
    accel_med: np.ndarray  # (3,) m/s^2
    accel_std: np.ndarray  # (3,) m/s^2
    accel_norm_med: float
    static: bool
    reject_reasons: list[str]
    temperature_mean: float | None


@dataclasses.dataclass
class StaticInterval:
    segment_index: int
    start: int
    end: int
    t0: float
    t1: float
    duration: float
    n_windows: int
    n_samples: int


def _mad_scale(x: np.ndarray) -> np.ndarray:
    """逐轴稳健尺度 1.4826*MAD；MAD 为 0（静态/零偏数据常见）时回退到普通 std。"""
    mad = np.median(np.abs(x - np.median(x, axis=0)), axis=0)
    s = 1.4826 * mad
    if np.any(s < 1e-12):
        s_plain = np.std(x, axis=0)
        s = np.where(s < 1e-12, np.maximum(s_plain, 1e-12), s)
    return s


def detect_spikes(
    gyro: np.ndarray, accel: np.ndarray, z_thresh: float, neighbor: int = 3
) -> dict:
    """全序列逐样本异常峰值检测（孤立毛刺判据）。

    稳健 z 分数：中位数 + k*MAD。为避免把**持续运动段**整体误报成峰值
    （运动段整体远离静止中位数，但其邻域同样偏离），要求命中点邻域
    （±neighbor 个样本）中大多数点不命中，即只统计孤立瞬态毛刺。
    运动段由滑窗方差准则负责排除，异常峰值在此处显式审计。
    """
    per_axis: list[dict] = []
    total = 0
    for name, x in (("gyro", gyro), ("accel", accel)):
        med = np.median(x, axis=0)
        scale = _mad_scale(x)
        z = np.abs(x - med) / scale
        raw = np.any(z > z_thresh, axis=1)
        n = len(raw)
        isolated = np.zeros(n, dtype=bool)
        for i in np.flatnonzero(raw):
            lo, hi = max(0, i - neighbor), min(n, i + neighbor + 1)
            local = np.concatenate([raw[lo:i], raw[i + 1 : hi]])
            # 邻域多数点不偏离 -> 孤立毛刺；邻域整体偏离 -> 持续运动，不报为峰值
            if len(local) == 0 or np.sum(local) < 0.5 * len(local):
                isolated[i] = True
        idxs = np.flatnonzero(isolated).tolist()
        per_axis.append(
            {
                "signal": name,
                "count": len(idxs),
                "indices": idxs,
                "max_abs_z": float(np.max(z)) if len(z) else 0.0,
            }
        )
        total += len(idxs)
    return {"threshold_z": float(z_thresh), "total_samples_flagged": total, "signals": per_axis}


def _build_windows(data: CleanData, cfg: RuntimeConfig) -> list[Window]:
    wins: list[Window] = []
    for seg in data.segments:
        t = data.time[seg.start : seg.end + 1]
        a = data.accel[seg.start : seg.end + 1]
        g = data.gyro[seg.start : seg.end + 1]
        tp = (
            data.temperature[seg.start : seg.end + 1]
            if data.temperature is not None
            else None
        )

        if len(t) < 2 or (t[-1] - t[0]) <= 0:
            continue
        fs = (len(t) - 1) / (t[-1] - t[0])
        wsize = max(2, int(round(cfg.window_seconds * fs)))
        step = max(1, int(round(wsize * (1.0 - cfg.window_overlap))))

        for s0 in range(0, max(1, len(t) - wsize + 1), step):
            e0 = min(s0 + wsize - 1, len(t) - 1)
            if e0 <= s0:
                break
            aw, gw, tw = a[s0 : e0 + 1], g[s0 : e0 + 1], (
                tp[s0 : e0 + 1] if tp is not None else None
            )
            g_med = np.median(gw, axis=0)
            g_std = np.std(gw, axis=0)
            a_med = np.median(aw, axis=0)
            a_std = np.std(aw, axis=0)
            a_norm = float(np.linalg.norm(a_med))

            reasons: list[str] = []
            if np.any(g_std > cfg.gyro_std_thresh):
                bad = ["x", "y", "z"][int(np.argmax(g_std > cfg.gyro_std_thresh))]
                reasons.append(f"gyro_std_exceeded(axis={bad})")
            if np.any(a_std > cfg.accel_std_thresh):
                bad = ["x", "y", "z"][int(np.argmax(a_std > cfg.accel_std_thresh))]
                reasons.append(f"accel_std_exceeded(axis={bad})")
            gravity = 9.80665
            if abs(a_norm - gravity) > cfg.gravity_mag_tol:
                reasons.append(
                    f"gravity_magnitude_off(norm={a_norm:.4f}, expect={gravity:.4f})"
                )

            wins.append(
                Window(
                    segment_index=seg.index,
                    start=seg.start + s0,
                    end=seg.start + e0,
                    t0=float(t[s0]),
                    t1=float(t[e0]),
                    n=e0 - s0 + 1,
                    gyro_med=g_med,
                    gyro_std=g_std,
                    accel_med=a_med,
                    accel_std=a_std,
                    accel_norm_med=a_norm,
                    static=not reasons,
                    reject_reasons=reasons,
                    temperature_mean=(
                        float(np.mean(tw)) if tw is not None else None
                    ),
                )
            )
    return wins


def _merge_intervals(windows: list[Window], cfg: RuntimeConfig) -> list[StaticInterval]:
    """对每个段内的静止布尔序列做形态学闭运算（填平 <= bridge 个非静止窗），
    再按最小持续时间过滤。"""
    intervals: list[StaticInterval] = []
    seg_ids = sorted({w.segment_index for w in windows})
    for sid in seg_ids:
        sw = [w for w in windows if w.segment_index == sid]
        flags = np.array([w.static for w in sw], dtype=bool)
        if not flags.any():
            continue
        closed = flags.copy()
        run_start = None
        for i, f in enumerate(flags):
            if not f:
                if run_start is None:
                    run_start = i
            else:
                if run_start is not None:
                    gap_len = i - run_start
                    left_ok = run_start > 0
                    right_ok = i < len(flags)
                    if gap_len <= cfg.bridge_max_gap_windows and left_ok and right_ok:
                        closed[run_start:i] = True
                    run_start = None

        i = 0
        while i < len(closed):
            if not closed[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(closed) and closed[j + 1]:
                j += 1
            duration = sw[j].t1 - sw[i].t0
            n_samples = sw[j].end - sw[i].start + 1
            if duration >= cfg.min_static_seconds:
                intervals.append(
                    StaticInterval(
                        segment_index=sid,
                        start=sw[i].start,
                        end=sw[j].end,
                        t0=sw[i].t0,
                        t1=sw[j].t1,
                        duration=float(duration),
                        n_windows=int(np.sum(closed[i : j + 1])),
                        n_samples=n_samples,
                    )
                )
            else:
                # 被闭运算保留但总时长不足：不算静止，回写原因
                for w in sw[i : j + 1]:
                    if w.static:
                        w.static = False
                        w.reject_reasons.append("interval_shorter_than_min_static")
            i = j + 1
    return intervals


def detect_static(
    data: CleanData, cfg: RuntimeConfig
) -> tuple[list[Window], list[StaticInterval], dict]:
    windows = _build_windows(data, cfg)
    intervals = _merge_intervals(windows, cfg)

    accepted_idx: set[int] = set()
    for iv in intervals:
        for k, w in enumerate(windows):
            if w.segment_index == iv.segment_index and iv.start <= w.start and w.end <= iv.end:
                accepted_idx.add(k)

    n_static = sum(1 for w in windows if w.static)
    reject_counter: dict[str, int] = {}
    for w in windows:
        for r in w.reject_reasons:
            key = r.split("(")[0]
            reject_counter[key] = reject_counter.get(key, 0) + 1

    summary = {
        "windows_total": len(windows),
        "windows_static_pass": n_static,
        "windows_in_static_intervals": len(accepted_idx),
        "static_intervals": len(intervals),
        "reject_reason_counts": reject_counter,
    }
    return windows, intervals, summary
