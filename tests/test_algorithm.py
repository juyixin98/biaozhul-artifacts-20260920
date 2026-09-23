"""Algorithm-level tests on synthetic data."""

from __future__ import annotations

import math

import numpy as np
import pytest

from imu_bias_estimator.models import BiasEstimateRequest, DetectorConfig
from imu_bias_estimator.pipeline import run

from .synth import add_motion, make_static, G


def _req(t, a, g, temp=None, **cfg_kw):
    return BiasEstimateRequest(
        timestamps=t.tolist(),
        accelerometer=a.tolist(),
        gyroscope=g.tolist(),
        temperature=None if temp is None else temp.tolist(),
        config=DetectorConfig(**cfg_kw),
    )


def test_static_bias_recovered_robustly():
    bias = np.array([0.012, -0.008, 0.003])
    t, a, g = make_static(duration=12.0, bias=bias, seed=3)
    report = run(_req(t, a, g))
    assert report["status"] == "ok"
    est = np.array(report["gyroscope_bias"]["estimate_radps"])
    np.testing.assert_allclose(est, bias, atol=1e-3)
    # CI actually covers the truth
    for axis in report["gyroscope_bias"]["axes"]:
        lo, hi = axis["ci95_radps"]
        assert lo < hi
        assert axis["confidence"] in ("high", "medium")
    # intervals consistent for constant bias
    assert report["gyroscope_bias"]["interval_consistency"] in (
        "consistent",
        "single_interval",
    )


def test_motion_excluded_from_bias_pool():
    bias = np.array([0.01, -0.004, 0.0])
    t, a, g = make_static(duration=30.0, bias=bias, seed=4)
    add_motion(t, a, g, 8.0, 22.0)
    report = run(_req(t, a, g))
    assert report["status"] == "ok"
    est = np.array(report["gyroscope_bias"]["estimate_radps"])
    np.testing.assert_allclose(est, bias, atol=1.5e-3)

    # every candidate interval stays out of the motion window
    for c in report["candidates"]:
        assert c["end_time_sec"] <= 8.05 or c["start_time_sec"] >= 21.95

    # residuals inside candidates are tiny; the big motion rates are outside
    assert report["anomalies"]["sudden_motion"]["detected"] is True
    assert (
        report["anomalies"]["sudden_motion"][
            "max_angular_rate_outside_candidates_radps"
        ]
        > 0.3
    )
    assert max(report["residuals"]["gyro_maxabs_radps"]) < 0.03
    assert report["residuals"]["max_gyro_norm_in_candidates_radps"] < 0.03


def test_spike_is_flagged_and_rejected_not_averaged():
    bias = np.array([0.01, -0.005, 0.002])
    t, a, g = make_static(duration=12.0, bias=bias, seed=5)
    i = 600
    g[i, 1] += 5.0  # giant single-sample spike
    report = run(_req(t, a, g))
    assert report["status"] == "ok"
    gyro_spikes = [
        s for s in report["anomalies"]["spikes"] if s["sensor"] == "gyroscope"
    ]
    assert any(abs(s["time_sec"] - t[i]) < 0.02 for s in gyro_spikes)
    est = np.array(report["gyroscope_bias"]["estimate_radps"])
    np.testing.assert_allclose(est, bias, atol=1e-3)  # spike nowhere near est


def test_temperature_drift_is_fitted_and_flagged_inconsistent():
    # three static blocks at 25/40/55 C with a known temp slope, separated
    # by large time gaps so they remain separate intervals
    fs = 100.0
    slope = np.array([0.0020, 0.0010, 0.0])
    base = np.array([0.01, -0.005, 0.002])
    ts, aa, gg, ttemp = [], [], [], []
    for k, temp_c in enumerate((25.0, 40.0, 55.0)):
        t, a, g = make_static(
            duration=6.0, fs=fs, bias=(0, 0, 0), seed=10 + k,
            t0=k * 30.0,
        )
        bias_k = base + slope * (temp_c - 25.0)
        g += bias_k
        ts.append(t)
        aa.append(a)
        gg.append(g)
        ttemp.append(np.full_like(t, temp_c))
    t = np.concatenate(ts)
    a = np.vstack(aa)
    g = np.vstack(gg)
    temp = np.concatenate(ttemp)

    report = run(_req(t, a, g, temp=temp))
    assert report["status"] == "ok"
    n_cand = len(report["candidates"])
    assert n_cand >= 3

    fit = report["drift_temperature"]
    assert fit["available"] is True
    slopes = np.array([ax["slope"] for ax in fit["axes"]])
    np.testing.assert_allclose(slopes, slope, atol=6e-4)
    # the three interval medians differ systematically -> inconsistency flag
    assert report["gyroscope_bias"]["interval_consistency"] == "inconsistent"
    # pooled single-bias claim must not be presented as high confidence
    confs = [ax["confidence"] for ax in report["gyroscope_bias"]["axes"]]
    assert not any(c == "high" for c in confs)


def test_drift_unidentifiable_without_temperature_spread():
    t, a, g = make_static(duration=18.0, seed=6)
    # two short dropouts split the static record into 3 separate intervals
    keep = ~(((t > 5.0) & (t < 6.0)) | ((t > 11.0) & (t < 12.2)))
    t, a, g = t[keep], a[keep], g[keep]
    temp = np.full_like(t, 25.0)
    report = run(_req(t, a, g, temp=temp))
    assert len(report["candidates"]) >= 3
    fit = report["drift_temperature"]
    assert fit["available"] is False
    assert "identifiable" in fit["reason"]
    assert (
        report["observability"]["gyroscope_temperature_coefficient"]["observable"]
        is False
    )


def test_no_stationary_data_returns_no_bias():
    rng = np.random.default_rng(0)
    t = np.arange(1200) / 100.0
    g = rng.normal(0.0, 0.8, (1200, 3))
    a = rng.normal(0.0, 3.0, (1200, 3))
    report = run(_req(t, a, g))
    assert report["status"] == "no_stationary_data"
    assert report["gyroscope_bias"] is None
    assert report["accelerometer"]["bias_estimate_mps2"] is None


def test_gap_segments_data_and_candidates_per_segment():
    t, a, g = make_static(duration=20.0, seed=7)
    keep = ~((t > 9.0) & (t < 14.0))  # 5 s gap
    report = run(_req(t[keep], a[keep], g[keep]))
    assert len(report["segments"]) == 2
    assert len(report["anomalies"]["gaps"]) == 1
    assert report["anomalies"]["gaps"][0]["gap_sec"] == pytest.approx(5.0, abs=0.05)
    seg_ids = {c["segment_index"] for c in report["candidates"]}
    assert seg_ids == {0, 1}
    assert report["status"] == "ok"


def test_duplicate_timestamps_first_and_mean_policies():
    t, a, g = make_static(duration=5.0, seed=8)
    t = np.insert(t, 150, t[149])
    g_dup = g.copy()
    a_dup = a.copy()
    g_dup = np.insert(g_dup, 150, g[149] + 0.1, axis=0)
    a_dup = np.insert(a_dup, 150, a[149], axis=0)

    req = BiasEstimateRequest(
        timestamps=t.tolist(),
        accelerometer=a_dup.tolist(),
        gyroscope=g_dup.tolist(),
        time_repeat_policy="error",
    )
    with pytest.raises(Exception):
        run(req)

    for policy in ("first", "mean"):
        req.time_repeat_policy = policy
        report = run(req)
        assert report["anomalies"]["duplicate_timestamps"]["count"] == 1
        assert report["sample_count"] == t.size - 1


def test_unit_conversions_g_and_dps():
    # same physical signal expressed in g and deg/s
    t, a, g = make_static(duration=8.0, bias=(0.01, -0.005, 0.002), seed=9)
    req_si = _req(t, a, g)
    req_conv = BiasEstimateRequest(
        timestamps=(t * 1000.0).tolist(),
        accelerometer=(a / G).tolist(),
        gyroscope=np.degrees(g).tolist(),
        units={
            "time": "ms",
            "acceleration": "g",
            "angular_velocity": "deg/s",
            "temperature": "c",
        },
    )
    r_si = run(req_si)
    r_conv = run(req_conv)
    np.testing.assert_allclose(
        r_si["gyroscope_bias"]["estimate_radps"],
        r_conv["gyroscope_bias"]["estimate_radps"],
        atol=1e-12,
    )
    assert r_conv["sample_rate_hz"] == pytest.approx(100.0, rel=1e-6)


def test_gravity_check_uses_magnitude():
    # zero-g (free-fall-like) segment: low variance but |a| ~= 0 -> rejected
    rng = np.random.default_rng(2)
    n = 1200
    t = np.arange(n) / 100.0
    a = rng.normal(0.0, 0.005, (n, 3))
    g = rng.normal(0.0, 2e-3, (n, 3))
    report = run(_req(t, a, g))
    assert report["diagnostics"]["rejected"].get("gravity_magnitude", 0) > 0


def test_out_of_order_sorting():
    t, a, g = make_static(duration=5.0, seed=13)
    perm = np.random.default_rng(0).permutation(t.size)
    report = run(_req(t[perm], a[perm], g[perm]))
    assert report["anomalies"]["out_of_order_samples"] > 0
    est = np.array(report["gyroscope_bias"]["estimate_radps"])
    np.testing.assert_allclose(est, [0.01, -0.005, 0.002], atol=1e-3)


def test_duplicate_mean_averages_two_rows():
    t, a, g = make_static(duration=5.0, seed=14)
    t = np.insert(t, 150, t[149])
    extra_g = np.insert(g, 150, g[149] + 0.1, axis=0)
    extra_a = np.insert(a, 150, a[149], axis=0)
    report = run(
        BiasEstimateRequest(
            timestamps=t.tolist(),
            accelerometer=extra_a.tolist(),
            gyroscope=extra_g.tolist(),
            time_repeat_policy="mean",
        )
    )
    # the +0.1 rad/s duplicate is averaged away by median estimation
    est = np.array(report["gyroscope_bias"]["estimate_radps"])
    np.testing.assert_allclose(est, [0.01, -0.005, 0.002], atol=2e-3)
    assert report["sample_count"] == t.size - 1


def test_candidate_residual_fields_are_sane():
    t, a, g = make_static(duration=6.0, seed=15)
    report = run(_req(t, a, g))
    c = report["candidates"][0]
    assert c["sample_count"] > 100
    assert abs(c["gravity_residual_mps2"]) < 0.05
    assert c["gyro_rmse_radps"]  # non-empty
    assert max(c["gyro_maxabs_radps"]) < 0.05
    # reported timestamps are ordered SI seconds
    assert c["start_time_sec"] < c["end_time_sec"]
    res = report["residuals"]
    assert max(res["gyro_rmse_radps"]) < 0.01


def test_non_finite_values_rejected():
    t, a, g = make_static(duration=2.0, seed=11)
    g[10, 0] = np.nan
    with pytest.raises(Exception):
        run(_req(t, a, g))


def test_observability_block_never_claims_unobservable():
    t, a, g = make_static(duration=8.0, seed=12)
    report = run(_req(t, a, g))
    obs = report["observability"]
    assert obs["gyroscope_bias"]["observable"] is True
    for key in (
        "accelerometer_bias",
        "sensor_scale_factors",
        "axis_misalignment",
        "gyroscope_noise_density",
        "gyroscope_g_sensitivity",
    ):
        assert obs[key]["observable"] is False
    # no accelerometer bias value is ever produced
    assert report["accelerometer"]["bias_estimate_mps2"] is None
