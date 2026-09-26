"""积分器验收测试：静止、匀速转动、恒加速度、可变间隔、固定偏置。

合成数据模型（与 integrator 约定一致）：
- 世界系重力 g = [0, 0, -9.81]
- 加速度计比力 f = R^T (a_world - g) + b_a
- 陀螺仪 omega_meas = omega_true + b_g
"""

import numpy as np

from imu_preintegration import ImuIntegrator, IntegrationConfig
from imu_preintegration import quaternion as quat

GRAVITY = np.array([0.0, 0.0, -9.81])


def make_static_stream(n=201, dt=0.01):
    t = np.arange(n) * dt
    gyro = np.zeros((n, 3))
    accel = np.tile(-GRAVITY, (n, 1))  # 静止时比力 = -g
    return t, gyro, accel


def test_static_stream_stays_at_origin():
    t, gyro, accel = make_static_stream()
    result = ImuIntegrator().integrate(t, gyro, accel)
    assert result.validation.ok
    assert np.allclose(result.positions, 0.0, atol=1e-9)
    assert np.allclose(result.velocities, 0.0, atol=1e-9)
    assert np.allclose(result.orientations[-1], [1.0, 0.0, 0.0, 0.0], atol=1e-12)


def test_static_with_variable_dt_stays_at_origin():
    t, gyro, accel = make_static_stream()
    # 可变时间间隔：随机但严格递增的时间戳
    rng = np.random.default_rng(42)
    t_var = np.concatenate([[0.0], np.cumsum(rng.uniform(0.001, 0.02, size=200))])
    result = ImuIntegrator().integrate(t_var, gyro, accel)
    assert result.validation.ok
    assert np.allclose(result.positions, 0.0, atol=1e-9)
    assert np.allclose(result.velocities, 0.0, atol=1e-9)


def test_constant_rotation_recovers_final_orientation():
    omega_z = 0.5  # rad/s
    duration = 2.0
    dt = 0.005
    n = int(duration / dt) + 1
    t = np.arange(n) * dt
    gyro = np.tile([0.0, 0.0, omega_z], (n, 1))
    accel = np.tile(-GRAVITY, (n, 1))  # 绕 z 转动不改变 z 轴比力

    result = ImuIntegrator().integrate(t, gyro, accel)
    assert result.validation.ok
    expected = quat.from_rotvec(np.array([0.0, 0.0, omega_z * duration]))
    # 四元数符号等价：比较旋转作用结果
    v = np.array([1.0, 2.0, 3.0])
    assert np.allclose(
        quat.rotate(result.orientations[-1], v), quat.rotate(expected, v), atol=1e-9
    )
    # 纯转动不应产生位移
    assert np.allclose(result.positions, 0.0, atol=1e-9)


def test_constant_rotation_about_x_with_gravity_compensation():
    # 绕 x 轴匀速转动时，比力在机体系随姿态变化：f = R^T (-g)
    omega_x = 0.3
    duration = 1.0
    dt = 0.002
    n = int(duration / dt) + 1
    t = np.arange(n) * dt
    gyro = np.tile([omega_x, 0.0, 0.0], (n, 1))
    accel = np.empty((n, 3))
    for k in range(n):
        q_true = quat.from_rotvec(np.array([omega_x * t[k], 0.0, 0.0]))
        accel[k] = quat.rotate(quat.conjugate(q_true), -GRAVITY)

    result = ImuIntegrator().integrate(t, gyro, accel)
    assert result.validation.ok
    expected = quat.from_rotvec(np.array([omega_x * duration, 0.0, 0.0]))
    v = np.array([0.4, -1.1, 2.0])
    assert np.allclose(
        quat.rotate(result.orientations[-1], v), quat.rotate(expected, v), atol=1e-6
    )
    # 真值速度/位置为零，梯形积分残差应很小
    assert np.allclose(result.velocities, 0.0, atol=1e-3)
    assert np.allclose(result.positions, 0.0, atol=1e-3)


def test_constant_acceleration_recovers_velocity_and_position():
    a_true = np.array([1.0, -0.5, 0.2])
    duration = 2.0
    dt = 0.01
    n = int(duration / dt) + 1
    t = np.arange(n) * dt
    gyro = np.zeros((n, 3))
    accel = np.tile(a_true - GRAVITY, (n, 1))  # f = a - g

    result = ImuIntegrator().integrate(t, gyro, accel)
    assert result.validation.ok
    assert np.allclose(result.velocities[-1], a_true * duration, atol=1e-9)
    assert np.allclose(result.positions[-1], 0.5 * a_true * duration**2, atol=1e-9)


def test_fixed_biases_are_compensated():
    gyro_bias = np.array([0.02, -0.01, 0.03])
    accel_bias = np.array([0.05, -0.02, 0.1])
    config = IntegrationConfig(gyro_bias=gyro_bias, accel_bias=accel_bias)
    t, gyro, accel = make_static_stream()
    gyro_biased = gyro + gyro_bias
    accel_biased = accel + accel_bias

    result = ImuIntegrator(config).integrate(t, gyro_biased, accel_biased)
    assert result.validation.ok
    assert np.allclose(result.positions, 0.0, atol=1e-9)
    assert np.allclose(result.velocities, 0.0, atol=1e-9)
    assert np.allclose(result.orientations[-1], [1.0, 0.0, 0.0, 0.0], atol=1e-9)


def test_uncompensated_bias_diverges():
    # 对照组：不给偏置配置时，固定陀螺偏置应导致姿态漂移
    gyro_bias = np.array([0.0, 0.0, 0.1])
    t, gyro, accel = make_static_stream()
    result = ImuIntegrator().integrate(t, gyro + gyro_bias, accel)
    angle = 2.0 * np.arccos(np.clip(abs(result.orientations[-1][0]), 0.0, 1.0))
    assert angle > 0.1  # 2 秒 * 0.1 rad/s ≈ 0.2 rad 漂移


def test_initial_state_is_respected():
    t, gyro, accel = make_static_stream(n=11)
    q0 = quat.from_rotvec(np.array([0.0, 0.0, np.pi / 2]))
    # 静止但机体已旋转：比力 = R^T (-g)
    accel_rot = np.tile(quat.rotate(quat.conjugate(q0), -GRAVITY), (11, 1))
    result = ImuIntegrator().integrate(
        t,
        gyro,
        accel_rot,
        initial_orientation=q0,
        initial_velocity=np.array([0.0, 0.0, 0.0]),
        initial_position=np.array([1.0, 2.0, 3.0]),
    )
    assert np.allclose(result.positions[-1], [1.0, 2.0, 3.0], atol=1e-9)
    v = np.array([1.0, 0.0, 0.0])
    assert np.allclose(
        quat.rotate(result.orientations[-1], v), quat.rotate(q0, v), atol=1e-9
    )


def test_duplicate_timestamps_are_skipped_and_counted():
    t, gyro, accel = make_static_stream(n=11)
    t_dup = t.copy()
    t_dup[5] = t_dup[4]  # 制造重复时间戳
    result = ImuIntegrator().integrate(t_dup, gyro, accel)
    assert result.skipped_intervals == 1
    codes = {i.code for i in result.validation.errors}
    assert "duplicate_timestamp" in codes
    # 跳过后状态仍保持静止
    assert np.allclose(result.positions, 0.0, atol=1e-9)
