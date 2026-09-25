"""测试公共构造：恒速（CV）模型与合成轨迹。"""

import numpy as np


def cv_model(dt=1.0, q=0.01, r=0.25):
    """一维恒速模型：状态 [位置, 速度]，仅观测位置。"""
    F = np.array([[1.0, dt], [0.0, 1.0]])
    Q = q * np.array([[dt**3 / 3.0, dt**2 / 2.0], [dt**2 / 2.0, dt]])
    H = np.array([[1.0, 0.0]])
    R = np.array([[r]])
    return F, Q, H, R


def simulate_cv(n_steps, dt=1.0, q=0.01, r=0.25, x0=(0.0, 1.0), seed=42):
    """合成恒速轨迹与含噪位置量测。返回 (真值状态, 量测)。"""
    F, Q, H, R = cv_model(dt, q, r)
    rng = np.random.default_rng(seed)
    x = np.array(x0, dtype=float)
    chol_Q = np.linalg.cholesky(Q + 1e-15 * np.eye(2))
    states, measurements = [], []
    for _ in range(n_steps):
        x = F @ x + chol_Q @ rng.standard_normal(2)
        z = H @ x + np.sqrt(r) * rng.standard_normal(1)
        states.append(x.copy())
        measurements.append(float(z[0]))
    return np.array(states), measurements
