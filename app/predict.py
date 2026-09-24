"""Apply a published model to a raw device counter reading."""

from __future__ import annotations

import numpy as np

from .fitting import prediction_bounds


def convert_counter(envelope: dict, raw_counter: float) -> dict:
    """Convert raw counter -> host time with hard uncertainty interval."""

    payload = envelope["payload"]
    m = payload["model"]
    warnings: list[str] = []

    x = m["unwrap_base"] + float(raw_counter) - float(m["counter_start"])
    if x < m["counter_fitted_min"] or x > m["counter_fitted_max"]:
        warnings.append("counter outside fitted range: extrapolating")

    point = m["alpha_point"] + m["beta_point"] * x
    lo, hi = prediction_bounds(
        x,
        np.asarray(m["interval_counters"], dtype=float),
        np.asarray(m["interval_lower"], dtype=float),
        np.asarray(m["interval_upper"], dtype=float),
        m["beta_lower"],
        m["beta_upper"],
    )

    fitted_span = m["counter_fitted_max"] - m["counter_fitted_min"]
    if fitted_span > 0:
        frac = (x - m["counter_fitted_min"]) / fitted_span
        if frac < -0.25 or frac > 1.25:
            warnings.append("extrapolation beyond 25% of fitted span")

    if lo < m["host_valid_from"] or hi > m["host_valid_to"]:
        # bound expansion is expected for extrapolation; only flag strongly
        if not warnings:
            warnings.append("predicted interval reaches past calibration window")

    return {
        "device_id": payload["device_id"],
        "version_id": payload["version_id"],
        "epoch": m["epoch"],
        "counter": float(raw_counter),
        "host_time": {"point": float(point), "lower": float(lo),
                      "upper": float(hi)},
        "warning": "; ".join(warnings) if warnings else None,
        "valid_from": payload["valid_from"],
        "valid_to": payload["valid_to"],
    }
