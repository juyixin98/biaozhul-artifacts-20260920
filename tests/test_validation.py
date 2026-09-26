"""数据校验测试：结构错误、重复时间戳、乱序、缺样、角速度单位错误。"""

import numpy as np
import pytest

from imu_integrator import (
    integrate_imu,
    stationary_samples,
    constant_rotation_samples,
)
from imu_integrator.validation import prepare_samples, validate_samples, IMUDataError


def _sample(t, g=(0, 0, 0), a=(0, 0, 9.81)):
    return {"t": t, "gyro": list(g), "accel": list(a)}


def test_empty_samples_rejected():
    with pytest.raises(IMUDataError) as exc:
        prepare_samples([])
    assert exc.value.code == "empty_data"


def test_missing_field_rejected():
    with pytest.raises(IMUDataError) as exc:
        prepare_samples([{"t": 0.0, "gyro": [0, 0, 0]}])
    assert exc.value.code == "missing_field"
    assert exc.value.index == 0


def test_wrong_vector_length_rejected():
    with pytest.raises(IMUDataError) as exc:
        prepare_samples([_sample(0.0), {"t": 0.01, "gyro": [0, 0], "accel": [0, 0, 9.81]}])
    assert exc.value.code == "invalid_field"
    assert exc.value.index == 1


def test_non_numeric_and_nan_rejected():
    with pytest.raises(IMUDataError):
        prepare_samples([_sample(0.0), {"t": 0.01, "gyro": ["x", 0, 0],
                                        "accel": [0, 0, 9.81]}])
    with pytest.raises(IMUDataError) as exc:
        prepare_samples([_sample(0.0), {"t": 0.01, "gyro": [0, 0, np.nan],
                                        "accel": [0, 0, 9.81]}])
    assert exc.value.code == "non_finite"


def test_duplicate_timestamp_rejected_by_default():
    samples = [_sample(0.0), _sample(0.01), _sample(0.01), _sample(0.02)]
    with pytest.raises(IMUDataError) as exc:
        prepare_samples(samples, max_dt=0.02)
    assert exc.value.code == "duplicate_timestamp"
    assert exc.value.index == 2


def test_duplicate_timestamp_can_be_dropped():
    samples = [_sample(0.0), _sample(0.01, g=(1, 0, 0)),
               _sample(0.01, g=(2, 0, 0)), _sample(0.02)]
    t, gyro, _ = prepare_samples(samples, drop_duplicates=True)
    assert len(t) == 3
    # 保留最先出现的一条
    np.testing.assert_allclose(gyro[1], [1, 0, 0])


def test_integrator_duplicate_timestamp_end_to_end():
    samples = stationary_samples(duration=0.05, dt=0.01)
    samples.insert(2, dict(samples[1]))  # 复制一条造成重复时间戳
    with pytest.raises(IMUDataError) as exc:
        integrate_imu(samples)
    assert exc.value.code == "duplicate_timestamp"
    # 开启丢弃后可正常积分：原 6 条样本去重后仍为 6 条，5 个区间
    r = integrate_imu(samples, drop_duplicates=True)
    assert r.intervals == 5


def test_non_monotonic_time_rejected_by_default():
    samples = [_sample(0.0), _sample(0.02), _sample(0.01)]
    with pytest.raises(IMUDataError) as exc:
        prepare_samples(samples)
    assert exc.value.code == "non_monotonic_time"
    assert exc.value.index == 2


def test_non_monotonic_time_can_be_sorted():
    samples = [_sample(0.02), _sample(0.0), _sample(0.01)]
    t, _, _ = prepare_samples(samples, sort=True)
    np.testing.assert_allclose(t, [0.0, 0.01, 0.02])


def test_missing_samples_gap_detected():
    """缺样/掉帧：相邻间隔超过 max_dt。"""
    samples = [_sample(0.0), _sample(0.01), _sample(0.01),
               _sample(0.25), _sample(0.26)]
    # 先去重再检测缺样
    with pytest.raises(IMUDataError) as exc:
        prepare_samples(samples, drop_duplicates=True, max_dt=0.05)
    assert exc.value.code == "missing_samples"
    assert exc.value.index == 2


def test_missing_samples_ok_when_gap_within_limit():
    samples = [_sample(0.0), _sample(0.01), _sample(0.03), _sample(0.04)]
    summary = validate_samples(samples, max_dt=0.05)
    assert summary["ok"] is True
    assert summary["count"] == 4


def test_gyro_unit_error_detected_when_deg_s_marked_as_rad_s():
    """典型单位错误：300 deg/s 数值（≈5.24 rad/s）当 rad/s 尚可蒙混，
    但 1000 deg/s 数值当 rad/s 必超量程。"""
    samples = [_sample(0.0), _sample(0.01, g=(1000.0, 0.0, 0.0))]
    with pytest.raises(IMUDataError) as exc:
        prepare_samples(samples)
    assert exc.value.code == "gyro_unit_error"
    assert exc.value.index == 1


def test_gyro_unit_error_resolved_by_declaring_deg_s():
    samples = [_sample(0.0), _sample(0.01, g=(1000.0, 0.0, 0.0))]
    t, gyro, _ = prepare_samples(samples, gyro_unit="deg/s")
    np.testing.assert_allclose(gyro[1], [np.deg2rad(1000), 0, 0])


def test_invalid_unit_string_rejected():
    with pytest.raises(IMUDataError) as exc:
        prepare_samples([_sample(0.0)], gyro_unit="rpm")
    assert exc.value.code == "invalid_unit"


def test_deg_s_rotation_full_pipeline():
    """端到端：deg/s 标注的匀速转动积分出正确总转角。"""
    samples = constant_rotation_samples(angular_velocity=[0, 0, 0.5],
                                        duration=1.0, dt=0.01)
    samples_deg = [
        {"t": s["t"],
         "gyro": (np.asarray(s["gyro"]) * 180.0 / np.pi).tolist(),
         "accel": s["accel"]}
        for s in samples
    ]
    r = integrate_imu(samples_deg, gyro_unit="deg/s")
    # 0.5 rad/s * 1 s
    np.testing.assert_allclose(r.final_orientation[3], np.sin(0.25), atol=1e-9)
    np.testing.assert_allclose(r.final_orientation[0], np.cos(0.25), atol=1e-9)


def test_custom_gyro_limit():
    samples = [_sample(0.0), _sample(0.01, g=(10.0, 0.0, 0.0))]
    with pytest.raises(IMUDataError) as exc:
        prepare_samples(samples, gyro_limit_rad_s=5.0)
    assert exc.value.code == "gyro_unit_error"


def test_validate_samples_summary():
    samples = stationary_samples(duration=0.3, dt=0.1)
    summary = validate_samples(samples)
    assert summary == {"ok": True, "count": 4, "duration": pytest.approx(0.3),
                       "gyro_unit": "rad/s"}
