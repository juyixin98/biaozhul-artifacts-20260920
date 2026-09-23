"""Stationary-window detection and robust gyro bias estimation.

Pipeline (all quantities SI):

1. Per contiguous time segment, slide fixed-size non-overlapping windows.
2. A window is a stationary candidate only when *all* tests pass:
   accelerometer sliding variance, gyroscope sliding variance, gravity
   magnitude match, and small mean angular rate.  A spike therefore can
   never pull the bias: it inflates variance (MAD rejects outliers) and, at
   the window level, fails the variance / mean-rate tests.
3. Adjacent accepted windows are bridged across short rejected gaps and
   merged into candidate intervals; intervals shorter than the configured
   minimum are dropped.
4. Gyroscope bias is estimated robustly (median + MAD) only over candidate
   samples.  Between-interval consistency is checked with a weighted
   chi-square, so temperature drift surfaces as inconsistency rather than
   being silently averaged away.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .models import DetectorConfig
from .preprocess import SegmentedData

Z95 = 1.959963984540054  # two-sided 95% normal quantile
_MAD_N = 1.4826  # MAD -> Gaussian sigma
_MEDIAN_SE_FACTOR = math.sqrt(math.pi / 2.0)  # asymptotic SE(median) = factor * sigma / sqrt(n)
_REDUCED_CHI2_LIMIT = 2.5  # interval-consistency threshold


def mad(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    return float(np.median(np.abs(x - np.median(x))))


def medmad(x: np.ndarray) -> tuple[float, float]:
    med = float(np.median(x))
    return med, float(np.median(np.abs(x - med)))


@dataclass
class Window:
    seg: int
    start: int
    end: int  # inclusive
    accepted: bool
    reject_reason: str | None
    accel_var_max: float
    gyro_var_max: float
    accel_norm_mean: float
    gyro_mean_norm: float


def detect_windows(data: SegmentedData, cfg: DetectorConfig) -> list[Window]:
    windows: list[Window] = []
    for seg_idx, (s, e, median_dt) in enumerate(data.segments):
        seg_len = e - s + 1
        if math.isnan(median_dt) or median_dt <= 0:
            win_n = seg_len
        else:
            win_n = max(cfg.min_window_samples, int(round(cfg.window_sec / median_dt)))
        hop = max(win_n // 2, 1)  # 50% overlap
        ws = s
        while ws <= e:
            we = min(ws + win_n - 1, e)
            n = we - ws + 1
            if n < cfg.min_window_samples:
                windows.append(
                    Window(seg_idx, ws, we, False, "too_few_samples",
                           np.nan, np.nan, np.nan, np.nan)
                )
                break
            a = data.accel[ws : we + 1]
            g = data.gyro[ws : we + 1]
            av = float(np.max(np.var(a, axis=0)))
            gv = float(np.max(np.var(g, axis=0)))
            an = float(np.mean(np.linalg.norm(a, axis=1)))
            gn = float(np.linalg.norm(np.mean(g, axis=0)))

            reason = None
            if av > cfg.accel_variance_thresh:
                reason = "accel_variance"
            elif gv > cfg.gyro_variance_thresh:
                reason = "gyro_variance"
            elif abs(an - cfg.gravity_magnitude) > cfg.gravity_tolerance:
                reason = "gravity_magnitude"
            elif gn > cfg.gyro_mean_thresh:
                reason = "gyro_mean"
            windows.append(
                Window(seg_idx, ws, we, reason is None, reason, av, gv, an, gn)
            )
            if we == e:
                break
            ws += hop
    return windows


def build_stationary_mask(n: int, windows: list[Window]) -> np.ndarray:
    """Union of accepted windows: a sample is stationary only if *some*
    fully-accepted window contains it."""
    mask = np.zeros(n, dtype=bool)
    for w in windows:
        if w.accepted:
            mask[w.start : w.end + 1] = True
    return mask


def merge_intervals(
    data: SegmentedData,
    mask: np.ndarray,
    cfg: DetectorConfig,
) -> list[tuple[int, int, int]]:
    """Group stationary runs into reported intervals.

    Two runs separated by a rejected stretch shorter than ``bridge_gap_sec``
    are reported as one interval (span merges), but the rejected samples are
    not added to the mask, so motion samples never enter the bias pool.
    """
    intervals: list[tuple[int, int, int]] = []
    for seg_idx, (s, e, _) in enumerate(data.segments):
        sub = mask[s : e + 1]
        if not sub.any():
            continue
        idx = np.where(sub)[0]
        runs: list[list[int]] = [[idx[0], idx[0]]]
        for j in idx[1:]:
            if j <= runs[-1][1] + 1:
                runs[-1][1] = j
                continue
            gap_time = float(data.time[s + j] - data.time[s + runs[-1][1]])
            if gap_time <= cfg.bridge_gap_sec:
                runs[-1][1] = j  # merge span; interior samples stay excluded
            else:
                runs.append([j, j])
        for r0, r1 in runs:
            start, end = s + r0, s + r1
            span = float(data.time[end] - data.time[start]) if end > start else 0.0
            used = int(np.sum(mask[start : end + 1]))
            if (
                span >= cfg.min_candidate_duration_sec
                and used >= cfg.min_window_samples
            ):
                intervals.append((seg_idx, start, end))
    intervals.sort(key=lambda x: x[1])
    return intervals


def interval_stats(
    data: SegmentedData,
    intervals: list[tuple[int, int, int]],
    mask: np.ndarray,
) -> list[dict]:
    out = []
    for i, (seg, s, e) in enumerate(intervals):
        idx = np.where(mask[s : e + 1])[0] + s
        a = data.accel[idx]
        g = data.gyro[idx]
        med = np.median(g, axis=0)
        mads = np.median(np.abs(g - med), axis=0)
        means = np.mean(g, axis=0)
        rmse = np.sqrt(np.mean((g - med) ** 2, axis=0))
        maxabs = np.max(np.abs(g - med), axis=0)
        accel_mean = np.mean(a, axis=0)
        accel_norm = float(np.linalg.norm(accel_mean))
        out.append(
            {
                "index": i,
                "segment": seg,
                "start": s,
                "end": e,
                "idx": idx,
                "start_time": float(data.time[s]),
                "end_time": float(data.time[e]),
                "duration": float(data.time[e] - data.time[s]) if e > s else 0.0,
                "n": idx.size,
                "mid_time": float(0.5 * (data.time[s] + data.time[e])),
                "mean_temp": (
                    float(np.mean(data.temp[idx]))
                    if data.temp is not None
                    else None
                ),
                "accel_mean": accel_mean,
                "accel_norm": accel_norm,
                "gravity_residual": accel_norm,  # compared with reference by caller
                "gyro_median": med,
                "gyro_mad": mads,
                "gyro_mean": means,
                "gyro_rmse": rmse,
                "gyro_maxabs": maxabs,
            }
        )
    return out


def estimate_bias(
    data: SegmentedData, stats: list[dict], cfg: DetectorConfig
) -> dict:
    """Robust pooled bias, per-axis interval consistency, confidence."""
    sample_idx = (
        np.concatenate([st["idx"] for st in stats]) if stats else np.array([], int)
    )
    g_all = data.gyro[sample_idx] if sample_idx.size else np.full((1, 3), np.nan)
    n = sample_idx.size
    total_duration = (
        sum(st["duration"] for st in stats) if stats else 0.0
    )

    pooled_median = np.median(g_all, axis=0)
    pooled_mad = np.median(np.abs(g_all - pooled_median), axis=0)
    m = len(stats)
    interval_medians = np.array([st["gyro_median"] for st in stats])  # (m,3)
    interval_mads = np.array([st["gyro_mad"] for st in stats])
    interval_ns = np.array([st["n"] for st in stats], dtype=np.float64)

    # Per-axis robust noise floor keeps weights finite when MAD == 0.
    noise_floor = np.maximum(pooled_mad * _MAD_N, 1e-6)
    sigma_i = np.maximum(interval_mads * _MAD_N, noise_floor)
    se_median_i = _MEDIAN_SE_FACTOR * sigma_i / np.sqrt(interval_ns)[:, None]
    weights = 1.0 / se_median_i**2
    weighted_mean = np.sum(weights * interval_medians, axis=0) / np.sum(
        weights, axis=0
    )

    chi2 = np.zeros(3)
    dof = m - 1
    if m >= 2:
        chi2 = np.sum(
            ((interval_medians - weighted_mean) / se_median_i) ** 2, axis=0
        )

    axis_inconsistent = (
        chi2 / max(dof, 1) > _REDUCED_CHI2_LIMIT
        if m >= 2
        else np.zeros(3, dtype=bool)
    )
    if m < 2:
        consistency = "single_interval"
    elif axis_inconsistent.any():
        consistency = "inconsistent"
    else:
        consistency = "consistent"

    # Standard error of the bias estimate: prefer between-interval scatter
    # (it captures drift the within-interval noise cannot); otherwise the
    # asymptotic standard error of the pooled median.
    se = np.empty(3)
    if m >= 2:
        between = np.std(interval_medians, axis=0, ddof=1) / math.sqrt(m)
        within = _MEDIAN_SE_FACTOR * noise_floor / math.sqrt(n)
        se = np.maximum(between, within)
    else:
        se = _MEDIAN_SE_FACTOR * noise_floor / math.sqrt(n)

    ci = np.column_stack([pooled_median - Z95 * se, pooled_median + Z95 * se])

    def grade(k: int) -> str:
        if axis_inconsistent[k]:
            return "low" if se[k] <= 0.05 else "none"
        if (
            se[k] <= 0.005
            and m >= 2
            and total_duration >= 2.0
        ):
            return "high"
        if se[k] <= 0.02 and total_duration >= 1.0:
            return "medium"
        if se[k] <= 0.05:
            return "low"
        return "none"

    axes = [
        {
            "se": float(se[k]),
            "mad": float(pooled_mad[k]),
            "ci": ci[k].tolist(),
            "confidence": grade(k),
        }
        for k in range(3)
    ]

    return {
        "bias_radps": pooled_median,
        "se": se,
        "axes": axes,
        "n": n,
        "m": m,
        "duration": total_duration,
        "chi2": chi2,
        "dof": dof,
        "weighted_mean": weighted_mean,
        "consistency": consistency,
        "sample_idx": sample_idx,
    }


def wls_drift(
    x: np.ndarray, y: np.ndarray, sigma: np.ndarray
) -> tuple[float, float, float, float, float]:
    """Weighted linear regression y ~ intercept + slope*x.

    Returns slope, slope_se, intercept, residual_std, r_squared.
    """
    w = 1.0 / sigma**2
    sw = np.sum(w)
    wx = np.sum(w * x)
    wxx = np.sum(w * x * x)
    wy = np.sum(w * y)
    wxy = np.sum(w * x * y)
    det = sw * wxx - wx * wx
    intercept = (wxx * wy - wx * wxy) / det
    slope = (sw * wxy - wx * wy) / det
    resid = y - (intercept + slope * x)
    var_slope = sw / det
    slope_se = math.sqrt(max(var_slope, 0.0))
    residual_std = float(np.sqrt(np.sum(w * resid**2) / sw))
    ybar = np.sum(w * y) / sw
    ss_tot = np.sum(w * (y - ybar) ** 2)
    r2 = 1.0 - np.sum(w * resid**2) / ss_tot if ss_tot > 0 else 0.0
    return (
        float(slope),
        float(slope_se),
        float(intercept),
        residual_std,
        float(r2),
    )


def drift_models(stats: list[dict], bias_est: dict, data: SegmentedData) -> tuple[dict, dict]:
    """Temperature and time drift linear models over interval medians."""
    m = len(stats)
    medians = np.array([st["gyro_median"] for st in stats]) if m else np.zeros((0, 3))
    mads = np.array([st["gyro_mad"] for st in stats]) if m else np.zeros((0, 3))
    ns = (
        np.array([st["n"] for st in stats], dtype=np.float64)
        if m
        else np.zeros(0)
    )
    pooled_mad = np.median(
        np.abs(data.gyro[bias_est["sample_idx"]] - bias_est["bias_radps"]),
        axis=0,
    ) if m else np.zeros(3)
    noise_floor = np.maximum(pooled_mad * _MAD_N, 1e-6)
    sigma = np.maximum(mads * _MAD_N, noise_floor)

    def empty(reason: str, variable: str) -> dict:
        return {
            "available": False,
            "independent_variable": variable,
            "reason": reason,
            "intervals": m,
            "spread": None,
            "quality": None,
            "axes": [],
        }

    # --- temperature model ---
    if data.temp is None:
        temp_fit = empty("temperature not supplied", "temperature_degC")
    elif m < 3:
        temp_fit = empty("need >= 3 stationary intervals", "temperature_degC")
    else:
        x = np.array([st["mean_temp"] for st in stats], dtype=np.float64)
        spread = float(x.max() - x.min())
        if spread < 2.0:
            temp_fit = empty(
                f"temperature spread {spread:.2f} C < 2 C: slope not identifiable",
                "temperature_degC",
            )
        else:
            axes = []
            for k, name in enumerate(["x", "y", "z"]):
                s, sse, b, rstd, r2 = wls_drift(x, medians[:, k], sigma[:, k])
                axes.append(
                    {"axis": name, "slope": s, "slope_se": sse,
                     "intercept": b, "residual_std": rstd, "r_squared": r2}
                )
            temp_fit = {
                "available": True,
                "independent_variable": "temperature_degC",
                "reason": None,
                "intervals": m,
                "spread": spread,
                "quality": "ok" if spread >= 10.0 else "marginal",
                "axes": axes,
            }

    # --- time model (bias change vs elapsed time: drift/run-up) ---
    if m < 3:
        time_fit = empty("need >= 3 stationary intervals", "elapsed_sec")
    else:
        x = np.array([st["mid_time"] for st in stats], dtype=np.float64)
        x = x - x.min()
        spread = float(x.max() - x.min())
        if spread < 10.0:
            time_fit = empty(
                f"time span {spread:.2f} s < 10 s: secular drift not identifiable",
                "elapsed_sec",
            )
        else:
            axes = []
            for k, name in enumerate(["x", "y", "z"]):
                s, sse, b, rstd, r2 = wls_drift(x, medians[:, k], sigma[:, k])
                axes.append(
                    {"axis": name, "slope": s, "slope_se": sse,
                     "intercept": b, "residual_std": rstd, "r_squared": r2}
                )
            time_fit = {
                "available": True,
                "independent_variable": "elapsed_sec",
                "reason": None,
                "intervals": m,
                "spread": spread,
                "quality": "ok" if spread >= 60.0 else "marginal",
                "axes": axes,
            }
    return temp_fit, time_fit


def find_spikes(data: SegmentedData, cfg: DetectorConfig) -> list[dict]:
    """Local Hampel-filter outliers (per segment, per axis), both sensors.

    A point is a spike only when it deviates strongly from a *quiet*
    neighbourhood: the local robust scale must be small (well below the
    stationary variance limits). This keeps isolated glitches inside static
    stretches while ignoring the edges and samples of sustained motion,
    whose neighbourhood moves.
    """
    spikes: list[dict] = []
    dts = [
        np.diff(data.time[s : e + 1])
        for s, e, _ in data.segments
        if e - s >= 2
    ]
    if not dts:
        return spikes
    fs = 1.0 / float(np.median(np.concatenate(dts)))
    half = int(round(0.25 * fs))
    half = max(5, min(half, 100))
    width = 2 * half + 1

    quiet_scale = {
        "accelerometer": math.sqrt(cfg.accel_variance_thresh) * 3.0,
        "gyroscope": math.sqrt(cfg.gyro_variance_thresh) * 3.0,
    }

    for s, e, _ in data.segments:
        block_len = e - s + 1
        if block_len < width + 4:
            continue
        for sensor, arr in (
            ("accelerometer", data.accel),
            ("gyroscope", data.gyro),
        ):
            block = arr[s : e + 1]
            padded = np.pad(block, ((half, half), (0, 0)), mode="edge")
            wins = np.lib.stride_tricks.sliding_window_view(
                padded, width, axis=0
            )  # (block_len, 3, width)
            med = np.median(wins, axis=2)
            mad = np.median(np.abs(wins - med[:, :, None]), axis=2)
            scale = np.maximum(_MAD_N * mad, 1e-12)
            dev = np.abs(block - med)
            z = np.nan_to_num(dev / scale, nan=0.0)

            # Spike = strong deviation AND quiet samples on *both* flanks.
            # The point must sit strictly inside the segment, so padded
            # edges and motion-run boundaries never qualify; a lone rejected
            # point at the edge of a static stretch still has two quiet
            # flanks and is correctly reported.
            flank = 10
            idx = np.arange(block_len)
            valid = (idx >= flank) & (idx < block_len - flank)
            close = (dev < quiet_scale[sensor]).astype(np.float64)
            cpad = np.pad(close, ((1, 0), (0, 0)), constant_values=0.0)
            pref = np.cumsum(cpad, axis=0)
            left = (
                pref[idx] - pref[np.maximum(0, idx - flank)]
            )
            right = (
                pref[np.minimum(block_len, idx + flank + 1)] - pref[idx + 1]
            )
            hit = (
                (z > cfg.spike_z_thresh)
                & (left == flank)
                & (right == flank)
                & valid[:, None]
            )
            ii, jj = np.where(hit)
            order = np.argsort(-z[ii, jj])
            for o in order[:10]:
                r, c = int(ii[o]), int(jj[o])
                spikes.append(
                    {
                        "index": s + r,
                        "time_sec": float(data.time[s + r]),
                        "sensor": sensor,
                        "axis": c,
                        "robust_z": float(z[r, c]),
                        "value": float(arr[s + r, c]),
                    }
                )
    return spikes
