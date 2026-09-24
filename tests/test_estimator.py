"""Unit tests for the estimation core and synthetic truth model."""
from __future__ import annotations

import numpy as np

from app.estimator import (
    FLAG_CALIBRATED,
    FLAG_CALIBRATION_SKIPPED_UNTRUSTED,
    FLAG_GAP_BEFORE,
    FLAG_SOC_SATURATED_LOW,
    FLAG_SOC_UNINITIALIZED,
    FLAG_TEMP_OUT_OF_TABLE,
    Sample,
    estimate_soc,
)
from app.synthetic import (
    scenario_bias_drift,
    scenario_charge_discharge,
    scenario_out_of_table_temp,
    scenario_sensor_dropout,
)


def test_discharge_then_charge_net_zero(params):
    synth = scenario_charge_discharge(params)
    result = estimate_soc(synth.samples, params, initial_soc=0.8)

    # With an explicit initial SOC every point is defined and in range.
    assert all(p.soc is not None and 0.0 <= p.soc <= 1.0 for p in result.points)

    # Two OCV calibrations (rest after discharge, rest after charge).
    assert len(result.calibrations) == 2
    cal_d, cal_c = result.calibrations
    # Discharge 0.5h @2A of a 10Ah pack -> about -0.1 SOC.
    assert abs(cal_d.soc_after - 0.70) < 0.01
    # Equal charge back (with charge efficiency < 1 it slightly undershoots).
    assert cal_c.soc_after > 0.79

    # Current sign convention: a negative current raises SOC.
    times = np.array([p.t_s for p in result.points])
    soc = np.array([p.soc if p.soc is not None else np.nan for p in result.points])
    charge = times > 2400
    assert np.nanmean(np.diff(soc[charge])) > 0.0
    # Final SOC stays inside the physical range.
    assert 0.0 <= result.final_soc <= 1.0


def test_bias_accumulation_then_ocv_correction(params):
    synth = scenario_bias_drift(params)
    result = estimate_soc(synth.samples, params)

    assert len(result.calibrations) == 2
    anchor, correction = result.calibrations
    assert abs(anchor.soc_after - 0.70) < 1e-9
    # Injected +0.05 A discharge bias makes Coulomb counting drift low.
    assert correction.soc_before < correction.soc_after
    # Bias evidence recovers the injected 0.05 A within 20%.
    assert abs(correction.bias_estimate_a - 0.05) < 0.01
    # Calibration snaps back onto the synthetic truth.
    assert abs(correction.soc_after - 0.60) < 0.005


def test_out_of_table_temperature_clamped_flagged_no_calibration(params):
    synth = scenario_out_of_table_temp(params)
    result = estimate_soc(synth.samples, params)

    assert FLAG_TEMP_OUT_OF_TABLE in result.flags
    assert FLAG_CALIBRATION_SKIPPED_UNTRUSTED in result.flags
    # Only the trusted 25C anchor calibrates; the 70C rest must not.
    assert len(result.calibrations) == 1
    assert result.calibrations[0].temp_c == 25.0
    # Estimation still proceeds using the clamped capacity factor.
    assert result.final_soc is not None
    assert result.final_soc < 0.6


def test_sensor_outage_is_gap_not_zero_current(params):
    synth = scenario_sensor_dropout(params)
    result = estimate_soc(synth.samples, params)

    assert result.n_gap_intervals == 1
    gap = result.gaps[0]
    assert gap.duration_s > 5.0 * 60
    assert FLAG_GAP_BEFORE in result.flags

    # Coulomb counting must skip the gap: SOC is flat across it.
    gap_point = next(p for p in result.points if FLAG_GAP_BEFORE in p.flags)
    before = next(p for p in reversed(result.points)
                  if p.t_s <= gap.t_start_s and p.soc is not None)
    assert abs(gap_point.soc - before.soc) < 1e-12

    # The gap increases the uncertainty budget; final OCV rest recalibrates.
    assert any(p.soc_sigma is not None and p.soc_sigma > 0.01 for p in result.points)
    final_cal = result.calibrations[-1]
    assert abs(final_cal.soc_after - 0.65) < 0.01


def test_no_integration_when_soc_uninitialized(params):
    # Samples with no initial SOC and no rest anchor: SOC stays undefined.
    samples = [Sample(t, 1.0, 3.8, 25.0) for t in range(0, 60)]
    result = estimate_soc(samples, params)
    assert result.final_soc is None
    assert all(p.soc is None for p in result.points)
    assert FLAG_SOC_UNINITIALIZED in result.flags


def test_soc_clamped_to_limits_with_flags(params):
    # Aggressive discharge from a low initial SOC: must saturate at 0.
    samples = [Sample(t, 10.0, 3.0, 25.0) for t in range(0, 4000)]
    result = estimate_soc(samples, params, initial_soc=0.05)
    assert result.final_soc == 0.0
    assert FLAG_SOC_SATURATED_LOW in result.flags


def test_current_sign_convention_explicit(params):
    # +1A for 1h on a 10Ah pack at 25C: SOC drops by exactly 0.1.
    dis = [Sample(float(t), 1.0, 3.8, 25.0) for t in range(0, 3601)]
    r = estimate_soc(dis, params, initial_soc=0.5)
    assert abs(r.final_soc - 0.4) < 1e-6

    chg = [Sample(float(t), -1.0, 3.8, 25.0) for t in range(0, 3601)]
    r = estimate_soc(chg, params, initial_soc=0.5)
    # Charge efficiency 0.985 -> SOC rises by 0.0985.
    assert abs(r.final_soc - (0.5 + 0.1 * 0.985)) < 1e-6
