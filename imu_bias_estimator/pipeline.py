"""End-to-end pipeline: request arrays -> validated response dict."""

from __future__ import annotations

import math

import numpy as np

from .estimator import (
    build_stationary_mask,
    detect_windows,
    drift_models,
    estimate_bias,
    find_spikes,
    interval_stats,
    merge_intervals,
)
from .models import BiasEstimateRequest
from .preprocess import preprocess

OBSERVABILITY = {
    "gyroscope_bias": (
        True,
        "Gyroscope zero-rate bias is directly observable: in any static "
        "attitude the true angular rate is zero, so the stationary median "
        "estimates bias without knowing orientation.",
    ),
    "accelerometer_bias": (
        False,
        "Accelerometer bias is NOT separable from attitude tilt. Static "
        "specific force equals R*g + b_a with unknown rotation R; only the "
        "gravity vector and its norm residual are reported, never a solved "
        "accelerometer bias.",
    ),
    "sensor_scale_factors": (
        False,
        "Scale-factor errors are unobservable from static data: the sensed "
        "magnitudes never vary (rate is always zero, specific force is only "
        "one gravity vector). Excitation/rotation maneuvers are required.",
    ),
    "axis_misalignment": (
        False,
        "Cross-axis misalignment is unobservable without multi-attitude or "
        "rotation data; a single static pose gives no independent axis "
        "excitation.",
    ),
    "gyroscope_noise_density": (
        False,
        "An Allan-variance noise-density/bias-instability characterization "
        "needs data across many correlation timescales. Only a within-"
        "window robust MAD noise proxy is provided here.",
    ),
    "gyroscope_g_sensitivity": (
        False,
        "Gyroscope acceleration sensitivity (deg/s/g) cannot be identified "
        "from a single static gravity direction.",
    ),
}


def run(req: BiasEstimateRequest) -> dict:
    timestamps = np.asarray(req.timestamps, dtype=np.float64)
    accel = np.asarray(req.accelerometer, dtype=np.float64)
    gyro = np.asarray(req.gyroscope, dtype=np.float64)
    temp = np.asarray(req.temperature, dtype=np.float64) if req.temperature is not None else None

    data = preprocess(
        timestamps,
        accel,
        gyro,
        temp,
        req.units,
        req.config,
        req.time_repeat_policy,
    )
    cfg = req.config
    n = data.time.shape[0]
    warnings: list[str] = []

    # ---- effective sample rate from within-segment deltas ----
    seg_dts = []
    for s, e, _ in data.segments:
        if e > s:
            seg_dts.append(np.diff(data.time[s : e + 1]))
    if seg_dts:
        median_dt = float(np.median(np.concatenate(seg_dts)))
        rate = 1.0 / median_dt if median_dt > 0 else 0.0
    else:
        rate = 0.0

    # ---- detection ----
    windows = detect_windows(data, cfg)
    mask = build_stationary_mask(n, windows)
    intervals = merge_intervals(data, mask, cfg)
    stats = interval_stats(data, intervals, mask)

    rejected: dict[str, int] = {}
    for w in windows:
        if not w.accepted and w.reject_reason:
            rejected[w.reject_reason] = rejected.get(w.reject_reason, 0) + 1

    # ---- spikes & sudden motion (whole signal, independent of detector) ----
    spikes_raw = find_spikes(data, cfg)
    g_axis = req.axes.gyroscope
    a_axis = req.axes.accelerometer
    spikes = []
    for sp in spikes_raw:
        labels = a_axis if sp["sensor"] == "accelerometer" else g_axis
        spikes.append({**sp, "axis": labels[sp["axis"]]})
    if spikes:
        warnings.append(
            f"{len(spikes)} robust outlier spike(s) detected and excluded "
            "via variance/MAD rejection"
        )

    non_stat = ~mask
    if non_stat.any():
        max_motion_rate = float(np.max(np.linalg.norm(data.gyro[non_stat], axis=1)))
    else:
        max_motion_rate = 0.0
    sudden_motion = {
        "detected": rejected.get("gyro_variance", 0) > 0
        or rejected.get("gyro_mean", 0) > 0
        or rejected.get("accel_variance", 0) > 0,
        "rejected_windows": rejected.get("gyro_variance", 0)
        + rejected.get("gyro_mean", 0)
        + rejected.get("accel_variance", 0),
        "max_angular_rate_outside_candidates_radps": max_motion_rate,
        "stationary_samples_pooled": int(mask.sum()),
        "nonstationary_samples_excluded": int(non_stat.sum()),
    }

    # ---- segment/gap/duplicate anomaly records ----
    segments_out = [
        {
            "index": k,
            "start_index": s,
            "end_index": e,
            "start_time_sec": float(data.time[s]),
            "end_time_sec": float(data.time[e]),
            "sample_count": e - s + 1,
            "median_dt_sec": (None if md is None or math.isnan(md) else md),
        }
        for k, (s, e, md) in enumerate(data.segments)
    ]
    gaps_out = [
        {
            "after_index": i0,
            "start_time_sec": t0,
            "end_time_sec": t1,
            "gap_sec": dt,
        }
        for i0, t0, t1, dt in data.gaps
    ]
    if data.gaps:
        warnings.append(
            f"{len(data.gaps)} time gap(s) split the data into "
            f"{len(data.segments)} segment(s); candidates are detected per "
            "segment and pooled assuming constant bias"
        )
    if data.duplicate_count:
        warnings.append(
            f"{data.duplicate_count} duplicate timestamp(s) merged with "
            f"policy '{req.time_repeat_policy}'"
        )
    if data.out_of_order_count:
        warnings.append(
            f"{data.out_of_order_count} sample(s) were not in timestamp "
            "order; data sorted before processing"
        )

    # ---- bias estimate ----
    bias_out = None
    residuals_out = None
    est = None
    status = "ok"
    if not stats:
        status = "no_stationary_data"
        warnings.append(
            "No interval passed all stationary tests; no bias is reported. "
            "Loosen thresholds only after inspecting the rejected reasons."
        )
    else:
        est = estimate_bias(data, stats, cfg)
        axis_results = []
        for k, name in enumerate(g_axis):
            axis_results.append(
                {
                    "axis": name,
                    "bias_radps": float(est["bias_radps"][k]),
                    "bias_degps": float(math.degrees(est["bias_radps"][k])),
                    "robust_mad_radps": float(est["axes"][k]["mad"]),
                    "standard_error_radps": float(est["se"][k]),
                    "ci95_radps": [float(v) for v in est["axes"][k]["ci"]],
                    "confidence": est["axes"][k]["confidence"],
                }
            )
        bias_out = {
            "estimate_radps": [float(v) for v in est["bias_radps"]],
            "estimate_degps": [float(math.degrees(v)) for v in est["bias_radps"]],
            "axes": axis_results,
            "method": "median of stationary-window samples (MAD-robust), "
            "interval consistency via weighted chi-square",
            "intervals_used": est["m"],
            "stationary_samples": est["n"],
            "stationary_duration_sec": float(est["duration"]),
            "weighted_chi_square": [float(v) for v in est["chi2"]],
            "chi_square_dof": int(est["dof"]),
            "interval_consistency": est["consistency"],
        }
        g_all = data.gyro[est["sample_idx"]]
        resid = g_all - est["bias_radps"]
        residuals_out = {
            "gyro_rmse_radps": [
                float(v) for v in np.sqrt(np.mean(resid**2, axis=0))
            ],
            "gyro_maxabs_radps": [float(v) for v in np.max(np.abs(resid), axis=0)],
            "max_gyro_norm_in_candidates_radps": float(
                np.max(
                    np.linalg.norm(
                        np.array([st["gyro_median"] for st in stats]), axis=1
                    )
                )
            ),
            "accel_gravity_residual_mps2": [
                float(st["accel_norm"] - cfg.gravity_magnitude) for st in stats
            ],
        }
        if est["consistency"] == "inconsistent":
            warnings.append(
                "Stationary intervals are statistically inconsistent "
                "(reduced chi^2 > 2.5): a single constant bias does not "
                "explain them — likely temperature/time drift. Per-axis "
                "confidence downgraded; inspect drift models."
            )
        if len({st["segment"] for st in stats}) > 1:
            warnings.append(
                "Candidates span segments separated by time gaps; pooling "
                "assumes bias is constant across the gap"
            )

    # ---- candidate interval records ----
    candidates_out = []
    for st in stats:
        candidates_out.append(
            {
                "index": st["index"],
                "segment_index": st["segment"],
                "start_index": st["start"],
                "end_index": st["end"],
                "start_time_sec": st["start_time"],
                "end_time_sec": st["end_time"],
                "duration_sec": float(st["duration"]),
                "sample_count": int(st["n"]),
                "mid_time_sec": st["mid_time"],
                "mean_temperature_c": st["mean_temp"],
                "accel_mean_mps2": [float(v) for v in st["accel_mean"]],
                "accel_norm_mps2": float(st["accel_norm"]),
                "gravity_residual_mps2": float(
                    st["accel_norm"] - cfg.gravity_magnitude
                ),
                "gyro_median_radps": [float(v) for v in st["gyro_median"]],
                "gyro_mad_radps": [float(v) for v in st["gyro_mad"]],
                "gyro_mean_radps": [float(v) for v in st["gyro_mean"]],
                "gyro_rmse_radps": [float(v) for v in st["gyro_rmse"]],
                "gyro_maxabs_radps": [float(v) for v in st["gyro_maxabs"]],
            }
        )

    # ---- accelerometer block (no solved bias) ----
    if mask.any():
        pooled_a = data.accel[mask]
        sf_mean = np.mean(pooled_a, axis=0)
        sf_norm = float(np.linalg.norm(sf_mean))
        accel_block = {
            "stationary_specific_force_mps2": [float(v) for v in sf_mean],
            "specific_force_norm_mps2": sf_norm,
            "gravity_reference_mps2": cfg.gravity_magnitude,
            "gravity_residual_mps2": sf_norm - cfg.gravity_magnitude,
            "axes": a_axis,
            "bias_estimate_mps2": None,
            "note": "Specific force R*g is measured in the body frame; without "
            "known attitude its mean is an attitude/gravity measurement, not "
            "an accelerometer bias.",
        }
    else:
        accel_block = {
            "stationary_specific_force_mps2": None,
            "specific_force_norm_mps2": None,
            "gravity_reference_mps2": cfg.gravity_magnitude,
            "gravity_residual_mps2": None,
            "axes": a_axis,
            "bias_estimate_mps2": None,
            "note": "No stationary samples; accelerometer bias remains unobservable.",
        }

    # ---- drift models ----
    if est is not None:
        temp_fit, time_fit = drift_models(stats, est, data)
    else:
        temp_fit = {
            "available": False,
            "independent_variable": "temperature_degC",
            "reason": "no stationary intervals",
            "intervals": 0,
            "spread": None,
            "quality": None,
            "axes": [],
        }
        time_fit = {
            "available": False,
            "independent_variable": "elapsed_sec",
            "reason": "no stationary intervals",
            "intervals": 0,
            "spread": None,
            "quality": None,
            "axes": [],
        }
    for fit, label in (
        (temp_fit, "temperature coefficient"),
        (time_fit, "secular time drift"),
    ):
        if fit["available"] and fit["quality"] == "marginal":
            warnings.append(
                f"{label} fit is marginal (small independent-variable "
                "spread); slopes have large uncertainty"
            )
        if not fit["available"] and fit["intervals"] > 0:
            warnings.append(f"{label} not fitted: {fit['reason']}")

    # ---- observability ----
    obs = {k: {"observable": v[0], "detail": v[1]} for k, v in OBSERVABILITY.items()}
    obs["gyroscope_temperature_coefficient"] = {
        "observable": bool(temp_fit["available"] and temp_fit["quality"] == "ok"),
        "detail": (
            "Fitted per axis (rad/s per deg C) from >=3 intervals spanning "
            ">=10 C. Marginal/unfitted when temperature spread is small: a "
            "slope then exists but is not statistically identifiable."
            if temp_fit["available"]
            else f"Not identifiable from this record: {temp_fit['reason']}."
        ),
    }
    obs["gyroscope_time_drift"] = {
        "observable": bool(time_fit["available"] and time_fit["quality"] == "ok"),
        "detail": (
            "Fitted per axis (rad/s per second) from interval medians over "
            ">=60 s. Marginal/unfitted over short spans."
            if time_fit["available"]
            else f"Not identifiable from this record: {time_fit['reason']}."
        ),
    }

    return {
        "status": status,
        "sample_count": n,
        "sample_rate_hz": float(rate),
        "segments": segments_out,
        "candidates": candidates_out,
        "gyroscope_bias": bias_out,
        "accelerometer": accel_block,
        "residuals": residuals_out,
        "drift_temperature": temp_fit,
        "drift_time": time_fit,
        "observability": obs,
        "diagnostics": {
            "windows_total": len(windows),
            "windows_accepted": sum(1 for w in windows if w.accepted),
            "stationary_sample_fraction": float(mask.sum()) / n,
            "rejected": rejected,
        },
        "anomalies": {
            "duplicate_timestamps": {
                "count": data.duplicate_count,
                "policy": req.time_repeat_policy,
            },
            "out_of_order_samples": data.out_of_order_count,
            "gaps": gaps_out,
            "spikes": spikes,
            "sudden_motion": sudden_motion,
        },
        "warnings": warnings,
        "units_echo": {
            "input": req.units.model_dump(),
            "internal": {
                "time": "s",
                "acceleration": "m/s^2",
                "angular_velocity": "rad/s",
                "temperature": "degC",
            },
            "gyro_bias_reported_in": ["rad/s", "deg/s"],
        },
    }
