"""Bias accumulation: uncertainty grows with integration time, resets on OCV."""
import math

import pytest

from app.engine import Sample, estimate


def _discharge(params, seconds, current=5.0):
    samples = [Sample(float(t), current, 3.8, 25.0) for t in range(seconds + 1)]
    return estimate(samples, params, initial_soc=0.5)


def test_bias_bound_grows_linearly(params):
    seconds = 3600
    res = _discharge(params, seconds)
    s = res["summary"]
    expected_bias = params.i_bias_bound_a * seconds / (3600.0 * params.nominal_capacity_ah)
    assert s["sigma_bias"] == pytest.approx(expected_bias, rel=1e-9)
    assert s["sigma_bias"] > 0.0


def test_random_walk_grows_with_sqrt_time(params):
    seconds = 3600
    res = _discharge(params, seconds)
    s = res["summary"]
    expected_var = params.sigma0**2 + params.sigma_rw_per_sqrt_hr**2 * seconds / 3600.0
    assert s["sigma_random"] == pytest.approx(math.sqrt(expected_var), rel=1e-9)


def test_sigma_monotonic_without_calibration(params):
    res = _discharge(params, 600)
    sigmas = [row["sigma"] for row in res["trace"]]
    assert all(b >= a for a, b in zip(sigmas, sigmas[1:]))


def test_longer_integration_means_larger_sigma(params):
    short = _discharge(params, 300)["summary"]["sigma"]
    long = _discharge(params, 3000)["summary"]["sigma"]
    assert long > short


def test_ocv_calibration_resets_uncertainty(params):
    samples = [Sample(float(t), 5.0, 3.8, 25.0) for t in range(0, 601)]
    samples += [Sample(float(t), 0.0, 3.78, 25.0) for t in range(601, 901)]
    res = estimate(samples, params, initial_soc=0.5)
    s = res["summary"]
    assert s["n_ocv_calibrations"] > 0
    assert s["sigma"] == pytest.approx(params.sigma_ocv, rel=1e-12)
    assert s["sigma_bias"] == 0.0
    assert s["sigma_unknown"] == 0.0
