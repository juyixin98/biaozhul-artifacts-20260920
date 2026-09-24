"""EKF 数学层测试：预测、Joseph 更新、对称性/半正定、不同测量模型。"""
from __future__ import annotations

import numpy as np

from app import ekf


def _cov_ok(P, tol=1e-9):
    sym = np.allclose(P, P.T, atol=tol)
    psd = np.min(np.linalg.eigvalsh(0.5 * (P + P.T))) >= -tol
    return sym and psd


def test_predict_grows_uncertainty_and_moves_position():
    x = np.array([0.0, 0.0, 1.0, 0.5])
    P = np.diag([1.0, 1.0, 0.1, 0.1])
    x2, P2 = ekf.predict(x, P, dt=1.0, q=1.0)
    assert np.allclose(x2[:2], [1.0, 0.5])
    assert np.allclose(x2[2:], [1.0, 0.5])
    # 长时间无测量，位置不确定性应增大
    _, P3 = ekf.predict(x, P, dt=10.0, q=1.0)
    assert P3[0, 0] > P2[0, 0]
    assert _cov_ok(P2) and _cov_ok(P3)


def test_joseph_update_reduces_uncertainty_and_is_psd():
    x = np.array([1.0, 2.0, 0.0, 0.0])
    P = np.diag([4.0, 4.0, 4.0, 4.0])
    R = np.diag([0.5, 0.5])
    z = np.array([1.2, 2.1])
    out = ekf.update(x, P, z, R, ekf.H_GNSS)
    assert out["P"][0, 0] < P[0, 0]
    assert out["nis"] >= 0.0
    assert np.allclose(np.asarray(out["S"]), np.asarray(out["S"]).T)
    assert _cov_ok(out["P"])


def test_odometry_and_gnss_models():
    x = np.array([1.0, 2.0, 0.5, -0.5])
    P = np.eye(4)
    g = ekf.update(x, P, np.array([1.1, 2.2]), np.eye(2) * 0.2, ekf.H_GNSS)
    o = ekf.update(x, P, np.array([0.6, -0.4]), np.eye(2) * 0.05, ekf.H_ODO)
    # GNSS 主要修正位置，里程计主要修正速度
    assert abs(g["x"][0] - x[0]) > abs(g["x"][2] - x[2])
    assert abs(o["x"][2] - x[2]) > abs(o["x"][0] - x[0])


def test_enforce_psd_repairs_negative_eigenvalue():
    bad = np.array([[1.0, 2.0], [2.0, 1.0]])  # 特征值 3, -1
    fixed, min_eig = ekf.enforce_psd(bad, floor=1e-10)
    assert min_eig < -0.9
    assert np.min(np.linalg.eigvalsh(fixed)) >= 1e-10 - 1e-14
    assert np.allclose(fixed, fixed.T)


def test_process_noise_monotonic_in_dt():
    q1 = ekf.process_noise(1.0, 1.0)
    q5 = ekf.process_noise(5.0, 1.0)
    assert q5[0, 0] > q1[0, 0] and q5[2, 2] > q1[2, 2]
    assert _cov_ok(q1) and _cov_ok(q5)
