"""Sensor dropout: gaps are not zero current."""
import pytest

from app.engine import F_GAP_BEFORE, Sample, estimate


def _gapped(params, gap_s=300.0):
    samples = [Sample(float(t), 5.0, 3.8, 25.0) for t in range(0, 101)]
    t0 = 100.0 + gap_s
    samples += [Sample(t0 + float(k), 5.0, 3.8, 25.0) for k in range(0, 101)]
    return estimate(samples, params, initial_soc=0.5), t0


def test_gap_flagged_and_logged(params):
    res, t0 = _gapped(params)
    row = next(r for r in res["trace"] if r["t_s"] == t0)
    assert F_GAP_BEFORE in row["flags"]
    gaps = [e for e in res["events"] if e["type"] == "gap"]
    assert len(gaps) == 1
    assert gaps[0]["detail"]["gap_s"] == pytest.approx(300.0)


def test_no_charge_integrated_across_gap(params):
    res, t0 = _gapped(params)
    before = next(r for r in res["trace"] if r["t_s"] == 100.0)
    after = next(r for r in res["trace"] if r["t_s"] == t0)
    # the sample right after the gap integrates nothing
    assert after["delta_soc"] == 0.0
    assert after["soc"] == pytest.approx(before["soc"], abs=1e-15)


def test_gap_grows_unknown_current_uncertainty(params):
    res, _ = _gapped(params, gap_s=300.0)
    expected = params.i_unknown_bound_a * 300.0 / (3600.0 * params.nominal_capacity_ah)
    assert res["summary"]["sigma_unknown"] == pytest.approx(expected, rel=1e-9)
    assert res["summary"]["n_gaps"] == 1


def test_no_calibration_inside_gap(params):
    # A gap inside what would look like a rest window must not calibrate:
    # rest is re-established only by contiguous samples.
    samples = [Sample(float(t), 0.0, 3.78, 25.0) for t in range(0, 61)]
    samples += [Sample(1000.0 + float(k), 0.0, 3.78, 25.0) for k in range(0, 61)]
    res = estimate(samples, params, initial_soc=0.9)
    # both rest segments are long enough individually, so calibration DOES
    # happen in each — but never "across" the gap: soc after the gap equals
    # the OCV-mapped value, not something integrated through the gap.
    assert res["summary"]["n_ocv_calibrations"] > 0
    assert res["summary"]["soc"] == pytest.approx(0.6, abs=1e-9)  # 3.78 V -> 0.6
    row_gap = next(r for r in res["trace"] if r["t_s"] == 1000.0)
    assert F_GAP_BEFORE in row_gap["flags"]
