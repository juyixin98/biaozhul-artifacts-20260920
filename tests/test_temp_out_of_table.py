"""Out-of-table temperatures: clamped capacity factor, flag, no OCV trust."""
import pytest

from app.engine import F_TEMP_OUT_OF_TABLE, Sample, capacity_factor, estimate


def test_factor_clamped_below_table(params):
    factor, out = capacity_factor(-30.0, params)
    assert out is True
    assert factor == pytest.approx(params.capacity_temp_factor[0])  # -20 C edge


def test_factor_clamped_above_table(params):
    factor, out = capacity_factor(60.0, params)
    assert out is True
    assert factor == pytest.approx(params.capacity_temp_factor[-1])  # 55 C edge


def test_in_table_not_flagged(params):
    factor, out = capacity_factor(25.0, params)
    assert out is False
    assert factor == pytest.approx(1.0)


def test_out_of_table_samples_flagged_and_clamped_capacity(params):
    samples = [Sample(float(t), 5.0, 3.6, -30.0) for t in range(0, 61)]
    res = estimate(samples, params, initial_soc=0.5)
    row = res["trace"][-1]
    assert F_TEMP_OUT_OF_TABLE in row["flags"]
    assert row["capacity_factor"] == pytest.approx(0.80)  # clamped to -20 C edge
    assert row["eff_capacity_ah"] == pytest.approx(40.0)
    assert res["summary"]["n_temp_out_of_table"] == 61
    # integration used the clamped (smaller) capacity -> larger |dSOC|
    expected = 0.5 - 5.0 * 60 / (3600.0 * 40.0)
    assert res["summary"]["soc"] == pytest.approx(expected, abs=1e-12)


def test_out_of_table_temperature_invalidates_rest_window(params):
    # Perfect rest voltage, but at -30 C: no OCV calibration may happen.
    samples = [Sample(float(t), 0.0, 3.78, -30.0) for t in range(0, 301)]
    res = estimate(samples, params, initial_soc=0.5)
    assert res["summary"]["n_ocv_calibrations"] == 0
    assert res["summary"]["soc"] == pytest.approx(0.5)  # untouched
