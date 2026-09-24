"""Kalman 滤波器单元测试：dt 真实参与、协方差行为、门控距离。"""

from __future__ import annotations

import numpy as np
import pytest

from app.kalman import ConstantVelocity2D


def test_state_transition_uses_dt():
    kf = ConstantVelocity2D()
    F1 = kf.state_transition(1.0)
    F3 = kf.state_transition(3.0)
    # px' = px + dt*vx
    assert F1[0, 2] == 1.0
    assert F3[0, 2] == 3.0
    assert F3[1, 3] == 3.0


def test_process_covariance_scales_with_dt():
    kf = ConstantVelocity2D(q=2.0)
    Q1 = kf.process_covariance(1.0)
    Q2 = kf.process_covariance(2.0)
    # 白加速度模型：位置-位置项 ~ q dt^3/3，速度-速度项 ~ q dt
    assert Q1.shape == (4, 4)
    assert Q2[0, 0] == pytest.approx(2.0 * 8.0 / 3.0)
    assert Q2[2, 2] == pytest.approx(2.0 * 2.0)
    assert Q1[0, 2] == pytest.approx(2.0 * 0.5)
    # 两个轴独立
    assert Q1[0, 1] == 0.0 and Q1[2, 3] == 0.0
    # 对称半正定
    assert np.allclose(Q1, Q1.T)
    assert np.linalg.eigvalsh(Q1).min() >= 0.0


def test_predict_position_scales_with_dt():
    kf = ConstantVelocity2D(q=0.0)
    x = np.array([1.0, 2.0, 0.5, -1.0])
    P = np.eye(4)
    x2, P2 = kf.predict(x, P, 2.0)
    assert x2[0] == pytest.approx(2.0)   # 1 + 2*0.5
    assert x2[1] == pytest.approx(0.0)   # 2 + 2*(-1)
    assert x2[2] == pytest.approx(0.5)
    # 预测后不确定性增长（q>0 时）
    kf_q = ConstantVelocity2D(q=1.0)
    _, P_grow = kf_q.predict(x, P, 2.0)
    assert P_grow[0, 0] > P[0, 0]


def test_predict_rejects_nonpositive_dt():
    kf = ConstantVelocity2D()
    x = np.zeros(4)
    P = np.eye(4)
    with pytest.raises(ValueError):
        kf.predict(x, P, 0.0)


def test_update_reduces_position_variance():
    kf = ConstantVelocity2D(q=0.5, r=1.0)
    x, P = kf.initialize(np.array([0.0, 0.0]))
    x_p, P_p = kf.predict(x, P, 1.0)
    x_u, P_u, nu, S = kf.update(x_p, P_p, np.array([0.2, -0.1]))
    assert P_u[0, 0] < P_p[0, 0]
    assert P_u[1, 1] < P_p[1, 1]
    # 新息就是观测与预测位置之差
    assert np.allclose(nu, np.array([0.2, -0.1]) - x_p[:2])
    assert S.shape == (2, 2)


def test_converges_on_constant_velocity_track():
    """在无噪匀速数据上滤波，稳态速度应逼近真值，误差很小。"""
    kf = ConstantVelocity2D(q=0.1, r=0.01)
    x, P = kf.initialize(np.array([0.0, 0.0]))
    for k in range(1, 60):
        x, P = kf.predict(x, P, 1.0)
        x, P, _, _ = kf.update(x, P, np.array([0.3 * k, 0.0]))
    assert x[2] == pytest.approx(0.3, abs=0.02)
    assert x[3] == pytest.approx(0.0, abs=0.02)
    assert x[0] == pytest.approx(0.3 * 59, abs=0.1)


def test_mahalanobis_and_euclidean():
    S = np.diag([4.0, 1.0])
    nu = np.array([2.0, 0.0])
    # d2 = (2^2)/4 = 1；欧氏 = 2
    assert ConstantVelocity2D.mahalanobis_sq(nu, S) == pytest.approx(1.0)
    assert ConstantVelocity2D.euclidean(nu) == pytest.approx(2.0)
