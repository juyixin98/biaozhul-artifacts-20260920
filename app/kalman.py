"""二维匀速（constant-velocity）Kalman 滤波器。

状态向量 x = [px, py, vx, vy]^T，观测 z = [px, py]^T。
离散状态转移使用真实的时间间隔 dt：

    x_k = F(dt) x_{k-1} + w,   w ~ N(0, Q(dt))
    z_k = H x_k + v,           v ~ N(0, R)

过程噪声采用连续白加速度模型（Wiener-process velocity），
Q 与 dt^3、dt^2、dt 成比例，因此不同帧间隔不会被错误地当作等间隔处理。
"""

from __future__ import annotations

import numpy as np

_STATE_DIM = 4
_MEAS_DIM = 2


class ConstantVelocity2D:
    """匀速 2D Kalman 滤波器。

    Parameters
    ----------
    q:
        连续过程噪声强度（加速度谱密度），越大越信任观测。
    r:
        各坐标轴观测噪声方差，R = r * I2。
    p_pos, p_vel:
        初始位置 / 速度协方差对角元素。
    """

    def __init__(
        self,
        q: float = 1.0,
        r: float = 1.0,
        p_pos: float = 10.0,
        p_vel: float = 100.0,
    ) -> None:
        if q < 0 or r <= 0:
            raise ValueError("q 必须非负，r 必须为正")
        self.q = float(q)
        self.r = float(r)
        self._H = np.array(
            [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]], dtype=np.float64
        )
        self._R = self.r * np.eye(_MEAS_DIM)
        self._p_pos = float(p_pos)
        self._p_vel = float(p_vel)

    @property
    def H(self) -> np.ndarray:
        return self._H

    @property
    def R(self) -> np.ndarray:
        """观测噪声协方差 R = r * I2。"""
        return self._R

    # ------------------------------------------------------------------ init
    def initialize(self, measurement: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """以首个观测初始化状态：位置取观测值，速度取 0。"""
        z = np.asarray(measurement, dtype=np.float64).reshape(2)
        x = np.array([z[0], z[1], 0.0, 0.0], dtype=np.float64)
        P = np.diag([self._p_pos, self._p_pos, self._p_vel, self._p_vel])
        return x, P

    def state_transition(self, dt: float) -> np.ndarray:
        """与时间间隔 dt 相关的状态转移矩阵 F(dt)。"""
        dt = float(dt)
        F = np.eye(_STATE_DIM)
        F[0, 2] = dt
        F[1, 3] = dt
        return F

    def process_covariance(self, dt: float) -> np.ndarray:
        """白加速度模型的过程协方差 Q(dt)。

        单个轴（位置、速度）的连续白噪声加速度离散化结果为
        q * [[dt^3/3, dt^2/2], [dt^2/2, dt]]，两个轴独立。
        状态按 (px, py, vx, vy) 排列，故轴内索引分别为 {0,2} 与 {1,3}。
        """
        dt = float(dt)
        q = self.q
        block = q * np.array(
            [[dt**3 / 3.0, dt**2 / 2.0], [dt**2 / 2.0, dt]],
            dtype=np.float64,
        )
        Q = np.zeros((_STATE_DIM, _STATE_DIM), dtype=np.float64)
        for rows in (0, 2), (1, 3):
            for a, ia in enumerate(rows):
                for b, ib in enumerate(rows):
                    Q[ia, ib] = block[a, b]
        return Q

    # ----------------------------------------------------------------- steps
    def predict(
        self, x: np.ndarray, P: np.ndarray, dt: float
    ) -> tuple[np.ndarray, np.ndarray]:
        """时间更新：先验状态 x^- 与协方差 P^-。"""
        if dt <= 0:
            raise ValueError(f"dt 必须为正，收到 {dt}")
        F = self.state_transition(dt)
        Q = self.process_covariance(dt)
        x_pred = F @ x
        P_pred = F @ P @ F.T + Q
        # 协方差必须保持对称正定（数值对称化）
        P_pred = 0.5 * (P_pred + P_pred.T)
        return x_pred, P_pred

    def update(
        self,
        x_pred: np.ndarray,
        P_pred: np.ndarray,
        measurement: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """量测更新，返回 (x_hat, P_hat, innovation, S)。

        innovation = z - H x^-（新息），S = H P^- H^T + R（新息协方差）。
        """
        z = np.asarray(measurement, dtype=np.float64).reshape(2)
        H = self._H
        R = self._R
        innovation = z - H @ x_pred
        S = H @ P_pred @ H.T + R
        S = 0.5 * (S + S.T)
        # Kalman 增益：K = P^- H^T S^{-1}
        K = P_pred @ H.T @ np.linalg.inv(S)
        x_hat = x_pred + K @ innovation
        # Joseph 形式协方差更新，保证对称半正定
        I_KH = np.eye(_STATE_DIM) - K @ H
        P_hat = I_KH @ P_pred @ I_KH.T + K @ R @ K.T
        P_hat = 0.5 * (P_hat + P_hat.T)
        return x_hat, P_hat, innovation, S

    # ----------------------------------------------------------------- gates
    @staticmethod
    def mahalanobis_sq(
        innovation: np.ndarray, S: np.ndarray
    ) -> float:
        """新息的平方 Mahalanobis 距离 d^2 = nu^T S^{-1} nu（自由度 2）。"""
        nu = np.asarray(innovation, dtype=np.float64).reshape(2)
        return float(nu @ np.linalg.solve(S, nu))

    @staticmethod
    def euclidean(innovation: np.ndarray) -> float:
        """新息的欧氏距离 ||nu||_2（仅位置分量）。"""
        nu = np.asarray(innovation, dtype=np.float64).reshape(2)
        return float(np.linalg.norm(nu))
