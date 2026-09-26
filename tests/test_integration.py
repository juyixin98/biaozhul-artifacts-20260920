"""积分器 + 合成数据的端到端核验（静止、匀速转动、恒加速度、变 dt、偏置）。"""

import numpy as np
import pytest

from imu_integrator import (
    integrate_imu,
    stationary_samples,
    constant_rotation_samples,
    constant_acceleration_samples,
    quat_from_angle_axis,
    quat_rotate,
)

GRAVITY = (0.0, 0.0, -9.81)
TOL = 1e-10


def test_stationary_level_stays_put():
    samples = stationary_samples(duration=2.0, dt=0.01, gravity=GRAVITY)
    r = integrate_imu(samples, gravity=GRAVITY)

    np.testing.assert_allclose(r.positions, 0.0, atol=TOL)
    np.testing.assert_allclose(r.velocities, 0.0, atol=TOL)
    np.testing.assert_allclose(r.orientations,
                               np.tile([1, 0, 0, 0], (len(r.times), 1)), atol=TOL)
    np.testing.assert_allclose(r.accel_world, 0.0, atol=TOL)
    assert r.intervals == len(samples) - 1


def test_stationary_with_known_biases_is_compensated():
    bg = [0.01, -0.02, 0.005]
    ba = [0.1, -0.05, 0.2]
    samples = stationary_samples(duration=1.0, dt=0.01, gravity=GRAVITY,
                                 gyro_bias=bg, accel_bias=ba)
    r = integrate_imu(samples, gravity=GRAVITY, gyro_bias=bg, accel_bias=ba)

    np.testing.assert_allclose(r.positions, 0.0, atol=TOL)
    np.testing.assert_allclose(r.velocities, 0.0, atol=TOL)
    np.testing.assert_allclose(r.orientations[:, 0], 1.0, atol=TOL)
    np.testing.assert_allclose(np.linalg.norm(r.orientations, axis=1), 1.0, atol=TOL)


def test_stationary_uncompensated_gyro_bias_drifts_attitude():
    """未扣除的陀螺偏置必须产生姿态漂移（反向验证偏置确实生效）。"""
    bg = [0.0, 0.0, 0.1]
    samples = stationary_samples(duration=1.0, dt=0.01, gravity=GRAVITY, gyro_bias=bg)
    r = integrate_imu(samples, gravity=GRAVITY)  # 故意不给 gyro_bias
    expected = quat_from_angle_axis(0.1 * 1.0, [0, 0, 1])
    np.testing.assert_allclose(r.final_orientation, expected, atol=1e-6)


@pytest.mark.parametrize("omega,total", [
    ([0.0, 0.0, 1.0], 1.0),
    ([0.0, 0.0, -2.5], 0.4),
    ([0.0, 0.0, 0.7], 3.0),
])
def test_constant_rotation_about_z_matches_analytic_angle(omega, total):
    w = np.asarray(omega)
    samples = constant_rotation_samples(angular_velocity=omega, duration=total,
                                        dt=0.01, gravity=GRAVITY)
    r = integrate_imu(samples, gravity=GRAVITY)

    expected = quat_from_angle_axis(float(np.linalg.norm(w)) * total,
                                    w / np.linalg.norm(w))
    np.testing.assert_allclose(r.final_orientation, expected, atol=1e-9)
    # 绕天向轴旋转不改变重力方向，原点保持不动
    np.testing.assert_allclose(r.positions, 0.0, atol=TOL)
    np.testing.assert_allclose(r.velocities, 0.0, atol=TOL)


def test_constant_rotation_with_variable_dt():
    """非均匀时间间隔：累计转角只取决于 omega*时间总和。"""
    dts = [0.02, 0.05, 0.01, 0.1, 0.03, 0.04]
    total = float(sum(dts))
    samples = constant_rotation_samples(angular_velocity=[0, 0, 2.0], duration=total,
                                        dt=dts, gravity=GRAVITY)
    assert [s["t"] for s in samples] == pytest.approx(
        [0.0] + list(np.cumsum(dts)), abs=1e-12
    )
    r = integrate_imu(samples, gravity=GRAVITY)
    expected = quat_from_angle_axis(2.0 * total, [0, 0, 1])
    np.testing.assert_allclose(r.final_orientation, expected, atol=1e-10)
    np.testing.assert_allclose(r.positions, 0.0, atol=TOL)


def test_constant_rotation_deg_per_s_input():
    """deg/s 输入经 gyro_unit 转换后与等价 rad/s 输入结果一致。"""
    total = 0.5
    samples_rad = constant_rotation_samples(angular_velocity=[0, 0, 0.5],
                                            duration=total, dt=0.01, gravity=GRAVITY)
    samples_deg = [
        {"t": s["t"],
         "gyro": (np.asarray(s["gyro"]) * 180.0 / np.pi).tolist(),
         "accel": s["accel"]}
        for s in samples_rad
    ]
    r1 = integrate_imu(samples_rad, gravity=GRAVITY)
    r2 = integrate_imu(samples_deg, gravity=GRAVITY, gyro_unit="deg/s")
    np.testing.assert_allclose(r2.orientations, r1.orientations, atol=1e-12)
    np.testing.assert_allclose(r2.positions, r1.positions, atol=1e-12)


def test_constant_rotation_about_horizontal_axis_angle_exact():
    """绕水平轴旋转：姿态角精确；位置存在一阶中点法残差，量级有界。"""
    omega = np.array([0.5, 0.0, 0.0])
    total = 1.0
    samples = constant_rotation_samples(angular_velocity=omega, duration=total,
                                        dt=1e-3, gravity=GRAVITY)
    r = integrate_imu(samples, gravity=GRAVITY)
    expected = quat_from_angle_axis(0.5, [1, 0, 0])
    np.testing.assert_allclose(r.final_orientation, expected, atol=1e-9)
    # 每区间中点近似残差 ~ g*(w dt/2)*dt，累计位置误差 O(g w dt T^2)
    assert np.linalg.norm(r.final_position) < 0.01


@pytest.mark.parametrize("a_world,duration,dt", [
    ([1.0, 0.0, 0.0], 2.0, 0.01),
    ([0.0, -2.0, 0.5], 1.5, 0.02),
    ([0.3, 0.4, 0.0], 3.0, [0.1, 0.2, 0.3, 0.15, 0.25]),
])
def test_constant_acceleration_matches_kinematics(a_world, duration, dt):
    a = np.asarray(a_world)
    if isinstance(dt, list):
        duration = float(sum(dt))
    samples = constant_acceleration_samples(acceleration_world=a, duration=duration,
                                            dt=dt, gravity=GRAVITY)
    r = integrate_imu(samples, gravity=GRAVITY)

    np.testing.assert_allclose(r.velocities[-1], a * duration, atol=1e-9)
    np.testing.assert_allclose(r.positions[-1], 0.5 * a * duration ** 2, atol=1e-9)
    np.testing.assert_allclose(r.accel_world, np.tile(a, (len(r.times), 1)), atol=1e-9)
    # 姿态保持单位四元数
    np.testing.assert_allclose(r.orientations[:, 0], 1.0, atol=1e-12)


def test_constant_acceleration_respects_initial_velocity():
    v0 = np.array([1.0, 0.0, -0.5])
    a = np.array([0.2, 0.0, 0.1])
    duration = 1.0
    samples = constant_acceleration_samples(acceleration_world=a, duration=duration,
                                            dt=0.02, gravity=GRAVITY)
    r = integrate_imu(samples, gravity=GRAVITY, initial_velocity=v0)
    np.testing.assert_allclose(r.final_velocity, v0 + a * duration, atol=1e-10)
    np.testing.assert_allclose(r.final_position,
                               v0 * duration + 0.5 * a * duration ** 2, atol=1e-10)


def test_tilted_constant_acceleration_rotated_into_world():
    """机身倾斜 90°（绕 y）：机体系 x 比力映射到世界系 -z 方向。"""
    q0 = quat_from_angle_axis(np.pi / 2, [0, 1, 0])
    a_body = np.array([1.0, 0.0, 0.0])  # 合成器按世界系加速度生成
    # 期望世界系加速度即传入值；这里验证旋转链自洽
    samples = constant_acceleration_samples(
        acceleration_world=quat_rotate(q0, a_body),
        duration=0.5, dt=0.01, gravity=GRAVITY, initial_orientation=q0)
    r = integrate_imu(samples, gravity=GRAVITY, initial_orientation=q0)
    np.testing.assert_allclose(r.accel_world[-1], quat_rotate(q0, a_body), atol=1e-9)


def test_single_sample_returns_initial_state():
    samples = stationary_samples(duration=1.0, dt=0.01, gravity=GRAVITY)[:1]
    r = integrate_imu(samples, gravity=GRAVITY, initial_position=[1, 2, 3])
    assert r.intervals == 0
    np.testing.assert_allclose(r.final_position, [1, 2, 3])
    np.testing.assert_allclose(r.final_velocity, 0.0)


def test_nonfinite_initial_orientation_rejected():
    samples = stationary_samples(duration=0.1, dt=0.01, gravity=GRAVITY)
    with pytest.raises(ValueError):
        integrate_imu(samples, gravity=GRAVITY,
                      initial_orientation=[0, 0, 0, 0])
