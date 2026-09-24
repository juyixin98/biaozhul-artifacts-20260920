"""OCV calibration in trusted rest windows."""
import pytest

from app.engine import F_OCV_CALIBRATED, F_REST, Sample, estimate, ocv_ref_25c, ocv_to_soc


def test_rest_window_calibrates_to_table(params):
    samples = [Sample(float(t), 0.0, 3.78, 25.0) for t in range(0, 121)]
    res = estimate(samples, params, initial_soc=0.9)
    assert res["summary"]["soc"] == pytest.approx(0.6, abs=1e-12)  # table: 3.78 V -> 0.6
    assert res["summary"]["n_ocv_calibrations"] == 61  # t=60..120 inclusive
    last = res["trace"][-1]
    assert F_REST in last["flags"] and F_OCV_CALIBRATED in last["flags"]
    assert res["summary"]["sigma"] == pytest.approx(params.sigma_ocv)


def test_short_rest_does_not_calibrate(params):
    samples = [Sample(float(t), 0.0, 3.78, 25.0) for t in range(0, 30)]  # < 60 s
    res = estimate(samples, params, initial_soc=0.9)
    assert res["summary"]["n_ocv_calibrations"] == 0
    assert res["summary"]["soc"] == pytest.approx(0.9)


def test_unstable_voltage_blocks_calibration(params):
    samples = []
    for t in range(0, 121):
        v = 3.78 if t % 2 == 0 else 3.79  # 10 mV steps > 5 mV limit
        samples.append(Sample(float(t), 0.0, v, 25.0))
    res = estimate(samples, params, initial_soc=0.9)
    assert res["summary"]["n_ocv_calibrations"] == 0


def test_current_above_rest_threshold_blocks_calibration(params):
    samples = [Sample(float(t), 0.3, 3.78, 25.0) for t in range(0, 121)]  # 0.3 A > 0.05 A
    res = estimate(samples, params, initial_soc=0.9)
    assert res["summary"]["n_ocv_calibrations"] == 0


def test_temperature_correction_applied(params):
    # At 45 C with dVdT = -0.0002 V/K: V_ref = 3.78 + 0.004 = 3.784 V
    samples = [Sample(float(t), 0.0, 3.78, 45.0) for t in range(0, 61)]
    res = estimate(samples, params, initial_soc=0.9)
    v_ref = ocv_ref_25c(3.78, 45.0, params)
    assert v_ref == pytest.approx(3.784, abs=1e-12)
    expected = ocv_to_soc(v_ref, params)
    assert expected == pytest.approx(0.605, abs=1e-9)  # interp between 3.78->0.6, 3.86->0.7
    assert res["summary"]["soc"] == pytest.approx(expected, abs=1e-12)
