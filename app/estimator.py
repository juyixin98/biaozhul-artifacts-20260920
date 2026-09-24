"""Offline SOC estimation core.

Model (explicitly an experimental/synthetic estimator - NOT for real charge
control):

* Sign convention: current > 0 means DISCHARGE, current < 0 means CHARGE
  (unit: ampere). Timestamps are seconds (epoch or relative), monotonic
  non-decreasing.
* Coulomb counting (trapezoidal integration):
      dSOC = -(I * eta(I)) * dt / (3600 * Q_nom * k_T(T))
  where eta = eta_discharge for I >= 0 else eta_charge, and k_T is the
  temperature capacity factor interpolated from the frozen table
  (clamped outside the table, flagged TEMP_OUT_OF_TABLE).
* Data gaps (dt > max_interval_s) are NOT treated as zero current: no
  integration is performed across a gap and uncertainty grows by a bounded
  term; points are flagged GAP_BEFORE.
* OCV calibration: in a trusted rest window (|I| below threshold for at
  least min_rest_duration_s, temperature inside the OCV-trusted range),
  the terminal voltage is treated as OCV and mapped to SOC through the
  frozen OCV table (linear interpolation, clamped). Calibration resets
  the bias-drift uncertainty term and estimates the accumulated current
  bias as evidence.
* SOC is always clamped to [0, 1]; saturation is flagged.
* Uncertainty output is a 1-sigma bound on SOC, combining measurement
  noise, random-walk bias growth since the last calibration/anchor, and
  bounded gap terms.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .params import Params

# Flag vocabulary (stable, part of the API contract).
FLAG_TEMP_OUT_OF_TABLE = "TEMP_OUT_OF_TABLE"
FLAG_GAP_BEFORE = "GAP_BEFORE"
FLAG_CALIBRATED = "CALIBRATED"
FLAG_CALIBRATION_SKIPPED_UNTRUSTED = "CALIBRATION_SKIPPED_UNTRUSTED"
FLAG_SOC_SATURATED_HIGH = "SOC_SATURATED_HIGH"
FLAG_SOC_SATURATED_LOW = "SOC_SATURATED_LOW"
FLAG_SOC_UNINITIALIZED = "SOC_UNINITIALIZED"
FLAG_REST = "REST"

SECONDS_PER_HOUR = 3600.0


@dataclass
class Sample:
    t_s: float
    current_a: float
    voltage_v: float
    temp_c: float


@dataclass
class PointEstimate:
    t_s: float
    soc: Optional[float]
    soc_sigma: Optional[float]
    flags: list[str] = field(default_factory=list)


@dataclass
class CalibrationEvent:
    t_s: float
    soc_before: Optional[float]
    soc_after: float
    ocv_v: float
    temp_c: float
    bias_estimate_a: float
    correction: Optional[float]


@dataclass
class GapEvent:
    t_start_s: float
    t_end_s: float
    duration_s: float


@dataclass
class EstimationResult:
    points: list[PointEstimate]
    calibrations: list[CalibrationEvent]
    gaps: list[GapEvent]
    n_samples: int
    n_integrated_intervals: int
    n_gap_intervals: int
    final_soc: Optional[float]
    final_soc_sigma: Optional[float]
    flags: list[str]

    def to_dict(self) -> dict:
        return {
            "n_samples": self.n_samples,
            "n_integrated_intervals": self.n_integrated_intervals,
            "n_gap_intervals": self.n_gap_intervals,
            "final_soc": self.final_soc,
            "final_soc_sigma": self.final_soc_sigma,
            "flags": self.flags,
            "calibrations": [vars(c) for c in self.calibrations],
            "gaps": [vars(g) for g in self.gaps],
            "points": [
                {"t_s": p.t_s, "soc": p.soc, "soc_sigma": p.soc_sigma, "flags": p.flags}
                for p in self.points
            ],
        }


def capacity_factor(temp_c: np.ndarray, params: Params) -> tuple[np.ndarray, np.ndarray]:
    """Temperature capacity factor k_T, clamped to the frozen table.

    Returns (k_T, out_of_table_mask). Out-of-table temperatures are clamped,
    never silently extrapolated.
    """
    t = np.asarray(temp_c, dtype=float)
    table_t = np.asarray(params.temp_table_c)
    table_f = np.asarray(params.temp_table_factor)
    out = (t < table_t[0]) | (t > table_t[-1])
    k = np.interp(t, table_t, table_f)  # np.interp clamps to endpoints
    return k, out


def ocv_to_soc(v_cell: np.ndarray, params: Params) -> np.ndarray:
    """Map cell OCV to SOC via the frozen table (linear, clamped to [0,1])."""
    v = np.asarray(v_cell, dtype=float)
    table_v = np.asarray(params.ocv_v_cell)
    table_soc = np.asarray(params.ocv_soc)
    order = np.argsort(table_v)
    return np.interp(v, table_v[order], table_soc[order])


def soc_to_ocv_cell(soc: np.ndarray, params: Params) -> np.ndarray:
    """Inverse map used by the synthetic data generator (truth model)."""
    s = np.asarray(soc, dtype=float)
    return np.interp(s, np.asarray(params.ocv_soc), np.asarray(params.ocv_v_cell))


def _rest_mask(samples: list[Sample], params: Params) -> np.ndarray:
    thr = params.rest_current_threshold_a
    return np.array([abs(s.current_a) <= thr for s in samples], dtype=bool)


def _rest_run_end_samples(rest: np.ndarray) -> np.ndarray:
    """For each index i, the number of consecutive rest samples ending at i."""
    n = len(rest)
    run = np.zeros(n, dtype=int)
    for i in range(n):
        run[i] = run[i - 1] + 1 if rest[i] else 0
    return run


def estimate_soc(samples: list[Sample], params: Params, initial_soc: Optional[float] = None) -> EstimationResult:
    """Run the offline estimation over a batch of samples.

    Samples must be sorted by timestamp (deduplicated upstream). Gaps are
    handled by skipping integration - never by assuming zero current.
    """
    n = len(samples)
    if n == 0:
        return EstimationResult([], [], [], 0, 0, 0, None, None, [])

    t = np.array([s.t_s for s in samples], dtype=float)
    cur = np.array([s.current_a for s in samples], dtype=float)
    volt = np.array([s.voltage_v for s in samples], dtype=float)
    temp = np.array([s.temp_c for s in samples], dtype=float)

    k_t, temp_out = capacity_factor(temp, params)
    rest = _rest_mask(samples, params)
    rest_run = _rest_run_end_samples(rest)

    q_nom_as = params.nominal_capacity_ah * SECONDS_PER_HOUR
    lo_trusted, hi_trusted = params.ocv_trusted_range_c

    soc: Optional[float] = None
    if initial_soc is not None:
        soc = min(1.0, max(0.0, float(initial_soc)))
    # sigma^2 decomposition: measurement noise + bias random walk + gap bound.
    var_meas = 0.0
    var_bias = 0.0
    var_gap = 0.0
    t_anchor: Optional[float] = None  # last calibration/anchor time
    sigma = params.initial_soc_sigma if soc is not None else None

    points: list[PointEstimate] = []
    calibrations: list[CalibrationEvent] = []
    gaps: list[GapEvent] = []
    session_flags: set[str] = set()
    n_integrated = 0
    n_gap = 0

    def total_sigma() -> Optional[float]:
        if soc is None:
            return None
        return float(min(1.0, np.sqrt(var_meas + var_bias + var_gap)))

    for i in range(n):
        flags: list[str] = []
        if temp_out[i]:
            flags.append(FLAG_TEMP_OUT_OF_TABLE)
            session_flags.add(FLAG_TEMP_OUT_OF_TABLE)
        if rest[i]:
            flags.append(FLAG_REST)

        if i > 0:
            dt = t[i] - t[i - 1]
            if dt > params.max_interval_s:
                # Data gap: do NOT integrate (gap is not zero current).
                n_gap += 1
                gaps.append(GapEvent(t_start_s=float(t[i - 1]), t_end_s=float(t[i]), duration_s=float(dt)))
                flags.append(FLAG_GAP_BEFORE)
                session_flags.add(FLAG_GAP_BEFORE)
                if soc is not None:
                    var_gap += (params.gap_sigma_bound_a * dt / (q_nom_as * k_t[i])) ** 2
            elif dt > 0.0 and soc is not None:
                i_avg = 0.5 * (cur[i - 1] + cur[i])
                eta = params.eta_discharge if i_avg >= 0.0 else params.eta_charge
                q_eff_as = q_nom_as * k_t[i]
                soc += -(i_avg * eta) * dt / q_eff_as
                n_integrated += 1
                var_meas += (params.current_noise_sigma_a * np.sqrt(dt) / q_eff_as) ** 2
                if t_anchor is not None:
                    var_bias = (
                        params.bias_growth_a_per_sqrt_s
                        * np.sqrt(max(0.0, t[i] - t_anchor))
                        / q_eff_as
                    ) ** 2
                if soc > 1.0:
                    soc = 1.0
                    flags.append(FLAG_SOC_SATURATED_HIGH)
                    session_flags.add(FLAG_SOC_SATURATED_HIGH)
                elif soc < 0.0:
                    soc = 0.0
                    flags.append(FLAG_SOC_SATURATED_LOW)
                    session_flags.add(FLAG_SOC_SATURATED_LOW)

        # OCV calibration at the moment a rest window first qualifies.
        if rest[i] and rest_run[i] >= 2:
            j = i - int(rest_run[i]) + 1  # window start
            window_duration = t[i] - t[j]
            prev_duration = t[i - 1] - t[j] if i > j else 0.0
            first_qualifies = (
                window_duration >= params.min_rest_duration_s
                and prev_duration < params.min_rest_duration_s
            )
            if first_qualifies:
                if lo_trusted <= temp[i] <= hi_trusted:
                    v_cell = volt[i] / params.n_series_cells
                    soc_ocv = float(ocv_to_soc(np.array([v_cell]), params)[0])
                    soc_before = soc
                    if soc is None:
                        correction = None
                        bias_est = 0.0
                    else:
                        correction = soc_ocv - soc
                        # Evidence: equivalent constant current bias since
                        # anchor. q_eff is in A*s, so bias = correction*Q/dt.
                        if t_anchor is not None and t[i] > t_anchor:
                            bias_est = correction * q_nom_as / (t[i] - t_anchor)
                        else:
                            bias_est = 0.0
                    soc = soc_ocv
                    var_meas = params.ocv_soc_sigma**2
                    var_bias = 0.0
                    var_gap = 0.0
                    t_anchor = float(t[i])
                    flags.append(FLAG_CALIBRATED)
                    session_flags.add(FLAG_CALIBRATED)
                    calibrations.append(
                        CalibrationEvent(
                            t_s=float(t[i]),
                            soc_before=soc_before,
                            soc_after=soc_ocv,
                            ocv_v=float(volt[i]),
                            temp_c=float(temp[i]),
                            bias_estimate_a=float(bias_est),
                            correction=correction,
                        )
                    )
                else:
                    flags.append(FLAG_CALIBRATION_SKIPPED_UNTRUSTED)
                    session_flags.add(FLAG_CALIBRATION_SKIPPED_UNTRUSTED)

        if soc is None:
            flags.append(FLAG_SOC_UNINITIALIZED)
            session_flags.add(FLAG_SOC_UNINITIALIZED)

        points.append(
            PointEstimate(t_s=float(t[i]), soc=soc, soc_sigma=total_sigma(), flags=flags)
        )

    return EstimationResult(
        points=points,
        calibrations=calibrations,
        gaps=gaps,
        n_samples=n,
        n_integrated_intervals=n_integrated,
        n_gap_intervals=n_gap,
        final_soc=soc,
        final_soc_sigma=total_sigma(),
        flags=sorted(session_flags),
    )
