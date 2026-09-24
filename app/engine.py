"""Offline SOC estimation engine.

Method (deterministic, no randomness, no I/O):

* Coulomb integration:  dSOC = -I * dt * eta / (3600 * Q_eff(T)).
  Sign convention: I > 0 = discharge (SOC decreases), I < 0 = charge.
  eta is the coulombic efficiency (charge < 1, discharge == 1 by default).
  Q_eff(T) = Q_nominal * capacity_factor(T), linear interpolation of the
  frozen temperature table; temperatures outside the table are clamped to
  the nearest edge factor AND flagged ``temp_out_of_table``.
* Data gaps (dt > gap_threshold_s) are NEVER treated as zero current.
  No charge is integrated across a gap; instead the unknown-current
  uncertainty bound grows with the gap duration and the sample after the
  gap is flagged ``gap_before``.
* Trusted rest windows: |I| <= rest_current_a for >= rest_min_duration_s
  with stable voltage (per-step and total-span limits) and in-table
  temperature. Inside such a window the OCV table (25 °C reference, with a
  linear dV/dT correction) maps terminal voltage to SOC and resets the
  uncertainty to sigma_ocv.
* Uncertainty model (conservative, documented — not a calibrated
  statistical interval):
    - random walk:  var_random += sigma_rw^2 * dt/3600   [per sqrt-hour]
    - current bias: sigma_bias  += i_bias_bound * dt / (3600 * Q_eff)
    - gap unknown:  sigma_unknown += i_unknown_bound * dt_gap / (3600 * Q_eff)
    - reported sigma = sqrt(var_random) + sigma_bias + sigma_unknown
  OCV calibration resets all three components to sigma_ocv.
* SOC is clamped to [soc_min, soc_max]; clamping is flagged and logged.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import numpy as np

from .params import Params

# Flag names (stable API vocabulary)
F_GAP_BEFORE = "gap_before"
F_TEMP_OUT_OF_TABLE = "temp_out_of_table"
F_SOC_CLAMPED_LOW = "soc_clamped_low"
F_SOC_CLAMPED_HIGH = "soc_clamped_high"
F_REST = "rest"
F_OCV_CALIBRATED = "ocv_calibrated"


@dataclass(frozen=True)
class Sample:
    t_s: float          # UTC epoch seconds
    current_a: float    # A, positive = discharge
    voltage_v: float    # V
    temp_c: float       # deg C


def capacity_factor(temp_c: float, p: Params) -> tuple[float, bool]:
    """Effective-capacity factor for a temperature.

    Returns (factor, out_of_table). Out-of-table temperatures are clamped to
    the nearest edge factor and reported so callers can flag them.
    """
    pts = p.capacity_temp_points_c
    out = temp_c < pts[0] or temp_c > pts[-1]
    t = min(max(temp_c, pts[0]), pts[-1])
    factor = float(np.interp(t, np.asarray(pts), np.asarray(p.capacity_temp_factor)))
    return factor, out


def ocv_to_soc(voltage_ref_25c: float, p: Params) -> float:
    """Map a 25 °C-referenced OCV to SOC via the frozen table (linear interp,
    clamped at table edges)."""
    return float(
        np.interp(voltage_ref_25c, np.asarray(p.ocv_volts), np.asarray(p.ocv_soc_points))
    )


def ocv_ref_25c(voltage_v: float, temp_c: float, p: Params) -> float:
    """Correct a measured rest voltage to the 25 °C reference:
    V_ref = V_meas - dVdT * (T - T_ref)."""
    return voltage_v - p.ocv_dv_dt_v_per_k * (temp_c - p.temp_ref_c)


def estimate(
    samples: Iterable[Sample],
    p: Params,
    initial_soc: Optional[float] = None,
    initial_sigma: Optional[float] = None,
) -> dict[str, Any]:
    """Run the estimator over time-ordered samples.

    ``samples`` must be sorted by t_s ascending with unique timestamps
    (the session store guarantees this). Returns a JSON-serializable dict
    with keys ``trace``, ``events``, ``summary``. Fully deterministic.
    """
    samples = list(samples)
    if initial_soc is None:
        soc = (p.soc_min + p.soc_max) / 2.0
        initial_source = "default_midpoint"
    else:
        soc = float(initial_soc)
        initial_source = "user"
    var_random = (p.sigma0 if initial_sigma is None else float(initial_sigma)) ** 2
    sigma_bias = 0.0
    sigma_unknown = 0.0

    trace: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    n_gaps = 0
    n_calibrations = 0
    n_temp_out = 0
    all_flags: set[str] = set()

    if soc < p.soc_min or soc > p.soc_max:
        clamped = min(max(soc, p.soc_min), p.soc_max)
        events.append({
            "t_s": samples[0].t_s if samples else None,
            "type": "initial_soc_clamped",
            "detail": {"requested": soc, "applied": clamped},
        })
        soc = clamped

    q_nom = p.nominal_capacity_ah
    prev_t: Optional[float] = None
    rest_start: Optional[float] = None
    rest_vmin = math.inf
    rest_vmax = -math.inf
    rest_vprev: Optional[float] = None
    rest_valid = True

    for s in samples:
        flags: list[str] = []
        dt = 0.0 if prev_t is None else s.t_s - prev_t
        gap = dt > p.gap_threshold_s

        factor, temp_out = capacity_factor(s.temp_c, p)
        q_eff = q_nom * factor
        if temp_out:
            flags.append(F_TEMP_OUT_OF_TABLE)
            n_temp_out += 1

        dsoc = 0.0
        if gap:
            # A gap is NOT zero current: integrate nothing, grow the
            # unknown-current bound instead.
            flags.append(F_GAP_BEFORE)
            n_gaps += 1
            sigma_unknown += p.i_unknown_bound_a * dt / (3600.0 * q_eff)
            events.append({
                "t_s": s.t_s,
                "type": "gap",
                "detail": {"gap_s": dt, "sigma_unknown_added": p.i_unknown_bound_a * dt / (3600.0 * q_eff)},
            })
        elif dt > 0.0:
            eta = p.coulomb_eff_charge if s.current_a < 0.0 else p.coulomb_eff_discharge
            dsoc = -s.current_a * dt * eta / (3600.0 * q_eff)
            soc += dsoc
            var_random += p.sigma_rw_per_sqrt_hr ** 2 * dt / 3600.0
            sigma_bias += p.i_bias_bound_a * dt / (3600.0 * q_eff)

        if soc < p.soc_min:
            events.append({"t_s": s.t_s, "type": "soc_clamp",
                           "detail": {"bound": p.soc_min, "raw_soc": soc}})
            soc = p.soc_min
            flags.append(F_SOC_CLAMPED_LOW)
        elif soc > p.soc_max:
            events.append({"t_s": s.t_s, "type": "soc_clamp",
                           "detail": {"bound": p.soc_max, "raw_soc": soc}})
            soc = p.soc_max
            flags.append(F_SOC_CLAMPED_HIGH)

        # --- trusted rest window detection + OCV calibration ---
        is_rest = abs(s.current_a) <= p.rest_current_a
        if is_rest:
            if rest_start is None:
                rest_start = s.t_s
                rest_vmin = rest_vmax = s.voltage_v
                rest_valid = True
            else:
                if rest_vprev is not None and abs(s.voltage_v - rest_vprev) > p.rest_voltage_max_step_v:
                    rest_valid = False
                rest_vmin = min(rest_vmin, s.voltage_v)
                rest_vmax = max(rest_vmax, s.voltage_v)
            if (rest_vmax - rest_vmin) > p.rest_voltage_max_span_v:
                rest_valid = False
            if temp_out and p.temp_out_of_range_invalidates_rest:
                rest_valid = False
            flags.append(F_REST)
            rest_duration = s.t_s - rest_start
            if rest_valid and rest_duration >= p.rest_min_duration_s:
                v_ref = ocv_ref_25c(s.voltage_v, s.temp_c, p)
                soc_cal = ocv_to_soc(v_ref, p)
                soc = min(max(soc_cal, p.soc_min), p.soc_max)
                var_random = p.sigma_ocv ** 2
                sigma_bias = 0.0
                sigma_unknown = 0.0
                n_calibrations += 1
                flags.append(F_OCV_CALIBRATED)
                events.append({
                    "t_s": s.t_s,
                    "type": "ocv_calibration",
                    "detail": {
                        "v_meas_v": s.voltage_v,
                        "temp_c": s.temp_c,
                        "v_ref_25c_v": v_ref,
                        "soc": soc,
                        "rest_duration_s": rest_duration,
                    },
                })
            rest_vprev = s.voltage_v
        else:
            if rest_start is not None and abs(s.current_a) > p.rest_exit_current_a:
                rest_start = None
                rest_vprev = None
                rest_vmin = math.inf
                rest_vmax = -math.inf
                rest_valid = True

        sigma = math.sqrt(var_random) + sigma_bias + sigma_unknown
        all_flags.update(flags)
        trace.append({
            "t_s": s.t_s,
            "dt_s": dt,
            "current_a": s.current_a,
            "voltage_v": s.voltage_v,
            "temp_c": s.temp_c,
            "capacity_factor": factor,
            "eff_capacity_ah": q_eff,
            "delta_soc": dsoc,
            "soc": soc,
            "sigma": sigma,
            "sigma_random": math.sqrt(var_random),
            "sigma_bias": sigma_bias,
            "sigma_unknown": sigma_unknown,
            "rest": is_rest,
            "flags": flags,
        })
        prev_t = s.t_s

    sigma = math.sqrt(var_random) + sigma_bias + sigma_unknown
    summary = {
        "n_samples": len(samples),
        "t_first_s": samples[0].t_s if samples else None,
        "t_last_s": samples[-1].t_s if samples else None,
        "duration_s": (samples[-1].t_s - samples[0].t_s) if len(samples) > 1 else 0.0,
        "initial_soc": float(initial_soc) if initial_soc is not None else (p.soc_min + p.soc_max) / 2.0,
        "initial_source": initial_source,
        "soc": soc,
        "sigma": sigma,
        "sigma_random": math.sqrt(var_random),
        "sigma_bias": sigma_bias,
        "sigma_unknown": sigma_unknown,
        "n_gaps": n_gaps,
        "n_ocv_calibrations": n_calibrations,
        "n_temp_out_of_table": n_temp_out,
        "flags": sorted(all_flags),
    }
    return {"trace": trace, "events": events, "summary": summary}
