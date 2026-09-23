"""Preprocessing: validation, unit conversion, duplicate handling, gap segmentation."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import InvalidInputError
from .models import DetectorConfig
from .units import (
    convert_accel,
    convert_gyro,
    convert_temperature,
    convert_time,
)


@dataclass
class SegmentedData:
    time: np.ndarray              # seconds, strictly increasing
    accel: np.ndarray             # m/s^2, shape (n, 3)
    gyro: np.ndarray              # rad/s,  shape (n, 3)
    temp: np.ndarray | None       # deg C,  shape (n,)
    segments: list[tuple[int, int, float]]  # (start, end_inclusive, median_dt_sec)
    gaps: list[tuple[int, float, float, float]]  # (idx_after, t_prev, t_next, gap_sec)
    duplicate_count: int
    out_of_order_count: int
    removed_indices: np.ndarray


def preprocess(
    timestamps: np.ndarray,
    accel: np.ndarray,
    gyro: np.ndarray,
    temp: np.ndarray | None,
    units,
    config: DetectorConfig,
    time_repeat_policy: str,
) -> SegmentedData:
    t = np.asarray(timestamps, dtype=np.float64)
    a = np.asarray(accel, dtype=np.float64)
    g = np.asarray(gyro, dtype=np.float64)

    if t.ndim != 1:
        raise InvalidInputError("timestamps must be a 1-D array")
    n = t.shape[0]
    if a.shape != (n, 3):
        raise InvalidInputError(
            f"accelerometer must have shape ({n}, 3), got {a.shape}"
        )
    if g.shape != (n, 3):
        raise InvalidInputError(
            f"gyroscope must have shape ({n}, 3), got {g.shape}"
        )
    if n == 0:
        raise InvalidInputError("at least one sample is required")
    for name, arr in (("timestamps", t), ("accelerometer", a), ("gyroscope", g)):
        if not np.all(np.isfinite(arr)):
            raise InvalidInputError(f"{name} contain NaN or infinite values")

    temp_c = None
    if temp is not None:
        temp_c = np.asarray(temp, dtype=np.float64)
        if temp_c.shape != (n,):
            raise InvalidInputError(
                f"temperature must have shape ({n},), got {temp_c.shape}"
            )
        if not np.all(np.isfinite(temp_c)):
            raise InvalidInputError("temperature contains NaN or infinite values")
        temp_c = convert_temperature(temp_c, units.temperature)

    t = convert_time(t, units.time)
    a = convert_accel(a, units.acceleration)
    g = convert_gyro(g, units.angular_velocity)

    # Reorder (capture how much reordering happened; exact pair-swap count is
    # reported as the number of displaced samples).
    order = np.argsort(t, kind="stable")
    out_of_order_count = int(np.sum(order != np.arange(n)))
    t = t[order]
    a = a[order]
    g = g[order]
    if temp_c is not None:
        temp_c = temp_c[order]

    # Duplicate timestamps (exact float equality).
    t, inverse = np.unique(t, return_inverse=True)
    duplicate_count = int(n - t.shape[0])
    if duplicate_count and time_repeat_policy == "error":
        raise InvalidInputError(
            f"{duplicate_count} duplicate timestamp(s); "
            "set time_repeat_policy to 'first' or 'mean' to handle them"
        )

    if duplicate_count:
        if time_repeat_policy == "first":
            first_mask = np.concatenate(
                ([True], inverse[1:] != inverse[:-1])
            )
            idx = np.where(first_mask)[0]
            groups = inverse[idx]
            a_new = np.empty((t.shape[0], 3), dtype=np.float64)
            g_new = np.empty_like(a_new)
            a_new[groups] = a[idx]
            g_new[groups] = g[idx]
            a, g = a_new, g_new
            if temp_c is not None:
                t_new = np.empty(t.shape[0], dtype=np.float64)
                t_new[groups] = temp_c[idx]
                temp_c = t_new
        else:  # mean
            a = _regroup_mean(a, inverse, t.shape[0])
            g = _regroup_mean(g, inverse, t.shape[0])
            temp_c = (
                _regroup_mean(temp_c, inverse, t.shape[0])
                if temp_c is not None
                else None
            )

    segments, gaps = _split_on_gaps(t, config)
    return SegmentedData(
        time=t,
        accel=a,
        gyro=g,
        temp=temp_c,
        segments=segments,
        gaps=gaps,
        duplicate_count=duplicate_count,
        out_of_order_count=out_of_order_count,
        removed_indices=np.array([], dtype=np.intp),
    )


def _regroup_mean(arr: np.ndarray, inverse: np.ndarray, m: int) -> np.ndarray:
    counts = np.bincount(inverse, minlength=m)
    sums = np.zeros((m,) + arr.shape[1:], dtype=np.float64)
    np.add.at(sums, inverse, arr)
    return sums / counts[(slice(None),) + (np.newaxis,) * (arr.ndim - 1)]


def _split_on_gaps(
    t: np.ndarray, config: DetectorConfig
) -> tuple[list[tuple[int, int, float]], list[tuple[int, float, float, float]]]:
    """Cut the (strictly increasing) timeline where dt exceeds the gap threshold.

    Threshold = min(max_gap_sec, gap_factor * median_dt).
    """
    if t.shape[0] == 1:
        return [(0, 0, np.nan)], []

    dt = np.diff(t)
    if np.any(dt <= 0):
        raise InvalidInputError("non-positive time delta remains after dedup")
    median_dt = float(np.median(dt))

    if config.max_gap_sec is not None:
        threshold = config.max_gap_sec
        if median_dt > 0:
            threshold = min(threshold, config.gap_factor * median_dt)
    else:
        threshold = config.gap_factor * median_dt

    gaps: list[tuple[int, float, float, float]] = []
    segments: list[tuple[int, int, float]] = []
    starts = [0]
    for i, d in enumerate(dt):
        if d > threshold:
            gaps.append((i + 1, float(t[i]), float(t[i + 1]), float(d)))
            starts.append(i + 1)

    bounds = starts + [t.shape[0]]
    for k in range(len(starts)):
        s, e = bounds[k], bounds[k + 1] - 1
        seg_dt = np.diff(t[s : e + 1])
        med = float(np.median(seg_dt)) if seg_dt.size else np.nan
        segments.append((s, e, med))
    return segments, gaps
