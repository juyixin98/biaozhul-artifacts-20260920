"""数据校验测试：缺样、重复时间、角速度单位可疑、形状错误。"""

import numpy as np

from imu_preintegration import validate_imu_stream


def make_clean_stream(n=101, dt=0.01):
    t = np.arange(n) * dt
    gyro = np.zeros((n, 3))
    accel = np.tile([0.0, 0.0, 9.81], (n, 1))
    return t, gyro, accel


def test_clean_stream_passes():
    t, gyro, accel = make_clean_stream()
    report = validate_imu_stream(t, gyro, accel)
    assert report.ok
    assert report.issues == []


def test_missing_samples_produce_gap_warning():
    t, gyro, accel = make_clean_stream()
    # 删除第 40~49 号样本，制造 0.11s 的空洞
    keep = np.array([k for k in range(len(t)) if not 40 <= k < 50])
    report = validate_imu_stream(t[keep], gyro[keep], accel[keep], max_dt=0.05)
    assert report.ok  # warning 不阻塞
    gaps = [i for i in report.warnings if i.code == "sample_gap"]
    assert len(gaps) == 1
    assert gaps[0].index is not None


def test_duplicate_timestamp_is_error():
    t, gyro, accel = make_clean_stream()
    t[10] = t[9]
    report = validate_imu_stream(t, gyro, accel)
    assert not report.ok
    assert any(i.code == "duplicate_timestamp" for i in report.errors)


def test_non_monotonic_timestamp_is_error():
    t, gyro, accel = make_clean_stream()
    t[20] = t[10]  # 回退
    report = validate_imu_stream(t, gyro, accel)
    assert not report.ok
    assert any(i.code == "non_monotonic_timestamp" for i in report.errors)


def test_gyro_in_deg_per_sec_triggers_unit_warning():
    t, gyro, accel = make_clean_stream()
    # 把 0.5 rad/s 误写成 deg/s 数值（≈28.6）
    gyro_deg = np.tile([0.0, 0.0, np.degrees(0.5)], (len(t), 1))
    report = validate_imu_stream(t, gyro_deg, accel)
    assert report.ok  # 单位可疑是 warning，不阻塞
    assert any(i.code == "gyro_unit_suspect" for i in report.warnings)


def test_reasonable_gyro_magnitude_no_warning():
    t, gyro, accel = make_clean_stream()
    gyro = np.tile([0.0, 0.0, 3.0], (len(t), 1))  # 3 rad/s，合理
    report = validate_imu_stream(t, gyro, accel)
    assert not any(i.code == "gyro_unit_suspect" for i in report.issues)


def test_shape_mismatch_is_error():
    t, gyro, accel = make_clean_stream()
    report = validate_imu_stream(t, gyro[:-1], accel)
    assert not report.ok
    assert any(i.code == "shape_mismatch" for i in report.errors)


def test_too_few_samples_is_error():
    t, gyro, accel = make_clean_stream(n=1, dt=0.01)
    report = validate_imu_stream(t, gyro, accel)
    assert not report.ok
    assert any(i.code == "too_few_samples" for i in report.errors)


def test_non_finite_values_are_error():
    t, gyro, accel = make_clean_stream()
    gyro[3, 0] = np.nan
    report = validate_imu_stream(t, gyro, accel)
    assert not report.ok
    assert any(i.code == "non_finite" for i in report.errors)
