"""核心算法测试：静止识别、零偏恢复、运动排除、峰值、缺口、单位/轴映射。"""

from __future__ import annotations

import numpy as np

from app.config import EstimateRequest, RuntimeConfig
from app.pipeline import run_pipeline

G = 9.80665


def _run(payload: dict) -> dict:
    req = EstimateRequest.model_validate(payload)
    return run_pipeline(req, RuntimeConfig.from_request(req))


def test_static_motion_static_recovers_bias(static_payload):
    res = _run(static_payload)
    gb = res["gyroscope_bias"]
    assert gb["status"] == "estimated"
    est = np.array(gb["bias_at_reference_rad_s"])
    truth = np.array([0.01, -0.02, 0.005])
    np.testing.assert_allclose(est, truth, atol=2e-3)
    # 两个静止区间，中间运动段不被包含
    assert len(res["static_intervals"]) == 2
    first, second = res["static_intervals"]
    assert first["t_end_s"] <= 15.5
    assert second["t_start_s"] >= 24.5


def test_motion_does_not_pollute_bias(static_payload):
    """把运动幅度放大一个量级，估计仍应稳定（运动不混入）。"""
    rng = np.random.default_rng(0)
    payload = {**static_payload}
    g = np.array(payload["gyro"])
    t = np.array(payload["timestamps"])
    m = (t >= 15) & (t < 25)
    g[m] += rng.normal(0, 5.0, (m.sum(), 3))
    payload["gyro"] = g.tolist()
    res = _run(payload)
    est = np.array(res["gyroscope_bias"]["bias_at_reference_rad_s"])
    np.testing.assert_allclose(est, [0.01, -0.02, 0.005], atol=3e-3)
    # 静止区间不覆盖运动时刻
    for iv in res["static_intervals"]:
        assert not (iv["t_start_s"] < 25 and iv["t_end_s"] > 15)


def test_temperature_drift_coefficients_recovered():
    fs = 100.0
    t = np.arange(0, 90, 1 / fs)
    temp = 20.0 + 0.25 * t
    rng = np.random.default_rng(1)
    b0 = np.array([0.0, 0.01, -0.01])
    kT = np.array([2.5e-4, -1.5e-4, 3.0e-4])
    bias = b0[None, :] + kT[None, :] * (temp - 20.0)[:, None]
    g = bias + rng.normal(0, 0.001, (len(t), 3))
    a = np.zeros((len(t), 3))
    a[:, 2] = G
    a += rng.normal(0, 0.005, (len(t), 3))
    res = _run(
        {
            "timestamps": t.tolist(),
            "accel": a.tolist(),
            "gyro": g.tolist(),
            "temperature": temp.tolist(),
        }
    )
    gb = res["gyroscope_bias"]
    assert all(v == "temp_drift" for v in gb["chosen_model_per_axis"].values())
    for i, kt in enumerate(kT):
        slope = gb["axes"]["xyz"[i]]["slope"]
        assert abs(slope - kt) < 2e-5, (i, slope, kt)


def test_isolated_spikes_detected_and_robust(rng):
    fs = 100.0
    t = np.arange(0, 30, 1 / fs)
    b = np.array([0.007, -0.004, 0.002])
    g = np.tile(b, (len(t), 1)) + rng.normal(0, 0.0015, (len(t), 3))
    a = np.zeros((len(t), 3))
    a[:, 2] = G
    a += rng.normal(0, 0.008, (len(t), 3))
    spike_idx = [314, 1500, 1501, 2450]
    for i in spike_idx:
        g[i] += np.array([6.0, -5.0, 7.0])
        a[i] += np.array([30.0, -25.0, 20.0])
    res = _run({"timestamps": t.tolist(), "accel": a.tolist(), "gyro": g.tolist()})
    flagged = {
        i for s in res["spikes"]["signals"] for i in s["indices"]
    }
    for i in spike_idx:
        assert i in flagged
    est = np.array(res["gyroscope_bias"]["bias_at_reference_rad_s"])
    np.testing.assert_allclose(est, b, atol=1e-3)


def test_duplicate_timestamps_consistent_merged(rng):
    fs = 100.0
    t = np.arange(0, 10, 1 / fs)
    b = np.array([0.01, 0.01, 0.01])
    g = np.tile(b, (len(t), 1)) + rng.normal(0, 0.001, (len(t), 3))
    a = np.zeros((len(t), 3))
    a[:, 2] = G
    a += rng.normal(0, 0.005, (len(t), 3))
    t = np.insert(t, [101], t[100])
    g = np.insert(g, [101], g[100], axis=0)
    a = np.insert(a, [101], a[100], axis=0)
    res = _run({"timestamps": t.tolist(), "accel": a.tolist(), "gyro": g.tolist()})
    assert res["preprocessing"]["duplicate_samples_merged"] == 1
    assert res["preprocessing"]["kept_samples"] == 1000
    np.testing.assert_allclose(
        res["gyroscope_bias"]["bias_at_reference_rad_s"], b, atol=1e-3
    )


def test_duplicate_timestamps_inconsistent_dropped(rng):
    fs = 100.0
    t = np.arange(0, 10, 1 / fs)
    b = np.array([0.01, 0.01, 0.01])
    g = np.tile(b, (len(t), 1)) + rng.normal(0, 0.001, (len(t), 3))
    a = np.zeros((len(t), 3))
    a[:, 2] = G
    a += rng.normal(0, 0.005, (len(t), 3))
    t = np.insert(t, [101], t[100])
    g_bad = g[100].copy()
    g_bad[0] += 5.0  # 同一时间戳数值严重不一致
    g = np.insert(g, [101], g_bad, axis=0)
    a = np.insert(a, [101], a[100], axis=0)
    res = _run({"timestamps": t.tolist(), "accel": a.tolist(), "gyro": g.tolist()})
    assert res["preprocessing"]["duplicate_samples_dropped_inconsistent"] == 2


def test_time_gap_segments_separately(rng):
    fs = 100.0
    t1 = np.arange(0, 8, 1 / fs)
    t2 = np.arange(120.0, 128.0, 1 / fs)  # 112s 缺口
    t = np.concatenate([t1, t2])
    b = np.array([0.005, -0.005, 0.002])
    g = np.tile(b, (len(t), 1)) + rng.normal(0, 0.001, (len(t), 3))
    a = np.zeros((len(t), 3))
    a[:, 2] = G
    a += rng.normal(0, 0.005, (len(t), 3))
    res = _run({"timestamps": t.tolist(), "accel": a.tolist(), "gyro": g.tolist()})
    assert res["preprocessing"]["segments"] == 2
    assert res["preprocessing"]["time_gaps"] == 1
    seg_with_static = [s for s in res["segments"] if s["n_static_windows"] > 0]
    assert len(seg_with_static) == 2


def test_no_static_returns_not_observable(rng):
    fs = 100.0
    t = np.arange(0, 15, 1 / fs)
    g = rng.normal(0, 0.5, (len(t), 3))
    a = rng.normal(0, 3.0, (len(t), 3))
    res = _run({"timestamps": t.tolist(), "accel": a.tolist(), "gyro": g.tolist()})
    assert res["gyroscope_bias"]["status"] == "not_observable_from_data"
    assert res["confidence"]["grade"] == "low"
    # 不可观测参数始终显式声明
    names = [p["parameter"] for p in res["unobservable_parameters"]]
    assert "accelerometer_bias" in names
    assert "gyroscope_scale_factor" in names


def test_units_degs_and_g_converted(rng):
    """角速度以 °/s、加速度以 g 输入，结果应与 SI 输入一致。"""
    fs = 100.0
    t = np.arange(0, 20, 1 / fs)
    b_rad = np.array([0.01, -0.02, 0.005])
    g = np.tile(b_rad, (len(t), 1)) + rng.normal(0, 0.002, (len(t), 3))
    a = np.zeros((len(t), 3))
    a[:, 2] = G
    a += rng.normal(0, 0.01, (len(t), 3))

    payload_si = {"timestamps": t.tolist(), "accel": a.tolist(), "gyro": g.tolist()}
    payload_conv = {
        "timestamps": (t * 1000.0).tolist(),  # ms
        "accel": (a / G).tolist(),           # g
        "gyro": np.degrees(g).tolist(),      # deg/s
        "units": {"accel": "g", "gyro": "degs", "time": "ms"},
    }
    e_si = np.array(_run(payload_si)["gyroscope_bias"]["bias_at_reference_rad_s"])
    e_cv = np.array(_run(payload_conv)["gyroscope_bias"]["bias_at_reference_rad_s"])
    np.testing.assert_allclose(e_cv, e_si, atol=1e-6)


def test_axes_remapping(rng):
    """输入列采用与内部系不同的排列/方向，配置轴映射后应恢复同一物理零偏。

    约定：axes.x=+z 表示内部 X 取输入 z 列，axes.z=-x 表示内部 Z 取输入 -x 列，
    因此输入列构造为：input(x)=-z_internal, input(y)=y_internal, input(z)=x_internal。
    """
    fs = 100.0
    t = np.arange(0, 20, 1 / fs)
    b = np.array([0.01, -0.02, 0.005])
    g = np.tile(b, (len(t), 1)) + rng.normal(0, 0.002, (len(t), 3))
    a = np.zeros((len(t), 3))
    a[:, 2] = G
    a += rng.normal(0, 0.01, (len(t), 3))

    g_perm = np.column_stack([-g[:, 2], g[:, 1], g[:, 0]])
    a_perm = np.column_stack([-a[:, 2], a[:, 1], a[:, 0]])
    payload = {
        "timestamps": t.tolist(),
        "accel": a_perm.tolist(),
        "gyro": g_perm.tolist(),
        "axes": {"x": "+z", "y": "+y", "z": "-x"},
    }
    res = _run(payload)
    est = np.array(res["gyroscope_bias"]["bias_at_reference_rad_s"])
    np.testing.assert_allclose(est, b, atol=2e-3)


def test_residuals_present_and_finite(static_payload):
    res = _run(static_payload)
    assert len(res["window_residuals"]) == res["detection_summary"][
        "windows_in_static_intervals"
    ]
    for wr in res["window_residuals"]:
        assert np.all(np.isfinite(wr["gyro_residual_rad_s"]))
        assert 0.0 <= min(wr["huber_weight_xyz"]) <= 1.0


def test_gravity_residual_reported(static_payload):
    res = _run(static_payload)
    for iv in res["static_intervals"]:
        assert abs(iv["gravity_magnitude_residual_ms2"]) < 0.25
