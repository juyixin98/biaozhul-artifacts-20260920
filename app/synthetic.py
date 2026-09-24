"""Synthetic battery telemetry generator.

Truth model (for generating data only): terminal voltage is
    V_pack = n_series * ( OCV_cell(soc_true, 25C) - I * R0 )
with the sign convention I > 0 = discharge. The estimator never sees
soc_true; tests compare the estimate against it.

This is a synthetic/experimental model - NOT for real charge control.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .estimator import Sample, soc_to_ocv_cell
from .params import Params

DEFAULT_R0_OHM = 0.01  # per-cell internal resistance of the synthetic cell


@dataclass
class SyntheticResult:
    samples: list[Sample]
    soc_true: np.ndarray  # true SOC trajectory at each emitted sample


def generate(
    params: Params,
    segments: list[tuple[float, float]],
    soc0: float = 0.8,
    dt_s: float = 1.0,
    temp_c: float = 25.0,
    r0_ohm: float = DEFAULT_R0_OHM,
    bias_a: float = 0.0,
    noise_a: float = 0.0,
    seed: int = 0,
    gaps: Optional[list[tuple[float, float]]] = None,
    temp_segments: Optional[list[tuple[float, float, float]]] = None,
) -> SyntheticResult:
    """Generate synthetic telemetry.

    segments: list of (duration_s, current_a) charge/discharge profile.
    bias_a: constant current-sensor bias added to measured current.
    noise_a: std of gaussian current measurement noise.
    gaps: list of (t_start_s, t_end_s) absolute-time ranges to drop
        (sensor outage - samples are removed, never zero-filled).
    temp_segments: optional list of (t_start_s, t_end_s, temp_c) overrides.
    """
    rng = np.random.default_rng(seed)
    samples: list[Sample] = []
    soc_true: list[float] = []
    soc = float(soc0)
    t = 0.0
    q_as = params.nominal_capacity_ah * 3600.0

    def in_gap(tt: float) -> bool:
        return any(g0 <= tt < g1 for g0, g1 in (gaps or []))

    def temp_at(tt: float) -> float:
        for t0, t1, tc in temp_segments or []:
            if t0 <= tt < t1:
                return tc
        return temp_c

    for duration, current in segments:
        steps = int(round(duration / dt_s))
        for _ in range(steps):
            # advance truth with the true current (no bias/noise)
            k_t = float(np.interp(temp_at(t), params.temp_table_c, params.temp_table_factor))
            eta = params.eta_discharge if current >= 0 else params.eta_charge
            soc = min(1.0, max(0.0, soc - current * eta * dt_s / (q_as * k_t)))
            if not in_gap(t):
                i_meas = current + bias_a + (float(rng.normal(0.0, noise_a)) if noise_a else 0.0)
                v_cell = float(soc_to_ocv_cell(np.array([soc]), params)[0]) - current * r0_ohm
                samples.append(
                    Sample(
                        t_s=float(t),
                        current_a=float(i_meas),
                        voltage_v=float(v_cell * params.n_series_cells),
                        temp_c=float(temp_at(t)),
                    )
                )
                soc_true.append(soc)
            t += dt_s
    return SyntheticResult(samples=samples, soc_true=np.asarray(soc_true))


# --- Named scenarios shared by examples, tests and the demo script ---


def scenario_charge_discharge(params: Params) -> SyntheticResult:
    """Discharge 0.5h @2A, rest 10min, charge 0.5h @-2A, rest 10min @25C."""
    return generate(
        params,
        segments=[(1800, 2.0), (600, 0.0), (1800, -2.0), (600, 0.0)],
        soc0=0.8,
        seed=1,
    )


def scenario_bias_drift(params: Params, bias_a: float = 0.05) -> SyntheticResult:
    """Constant +0.05A sensor bias during discharge; rest window lets OCV
    calibration correct the accumulated offset."""
    return generate(
        params,
        segments=[(120, 0.0), (3600, 1.0), (900, 0.0)],
        soc0=0.7,
        bias_a=bias_a,
        seed=2,
    )


def scenario_out_of_table_temp(params: Params) -> SyntheticResult:
    """Anchor at 25C rest, then discharge/rest at 70C (outside the
    -20..60C capacity-factor table and the 10..40C OCV-trusted range):
    capacity factor is clamped and flagged, OCV calibration is refused."""
    return generate(
        params,
        segments=[(120, 0.0), (600, 2.0), (300, 0.0)],
        soc0=0.6,
        temp_c=25.0,
        temp_segments=[(120.0, 1020.0, 70.0)],
        seed=3,
    )


def scenario_sensor_dropout(params: Params) -> SyntheticResult:
    """Trusted rest anchor, discharge with a 10-minute sensor outage
    (samples removed, not zeroed), continue discharge, final rest for
    re-calibration."""
    return generate(
        params,
        segments=[(120, 0.0), (1800, 2.0), (1800, 1.0), (600, 0.0)],
        soc0=0.8,
        gaps=[(720.0, 1320.0)],
        seed=4,
    )


def scenario_mixed(params: Params) -> SyntheticResult:
    """Mixed demo profile: discharge, rest, charge, a gap, and a cold spell."""
    return generate(
        params,
        segments=[(1200, 3.0), (600, 0.0), (900, -2.0), (300, 0.0), (1200, 1.5), (600, 0.0)],
        soc0=0.75,
        noise_a=0.01,
        gaps=[(3900.0, 4200.0)],
        temp_segments=[(3600.0, 4800.0, 5.0)],
        seed=5,
    )
