"""二维位置与速度 EKF（恒速模型）。

状态 x = [px, py, vx, vy]^T
- 预测：常速 + 连续白噪声加速度过程噪声 Q
- 里程计测量：z = [vx, vy]，H_odo
- GNSS 测量：  z = [px, py]，H_gnss

协方差更新全部采用 Joseph 形式 P = (I-KH) P (I-KH)^T + K R K^T，
配合对称化和特征值下限裁剪，保证数值稳定、对称且半正定。
"""
from __future__ import annotations

import numpy as np

STATE_DIM = 4
MEAS_DIM = 2

H_GNSS = np.array(
    [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]], dtype=np.float64
)
H_ODO = np.array(
    [[0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]], dtype=np.float64
)
I4 = np.eye(STATE_DIM, dtype=np.float64)

# 协方差特征值绝对下限：内部协方差绝不允许出现负/零特征值
PSD_FLOOR = 1e-12


def symmetrize(P: np.ndarray) -> np.ndarray:
    """强制对称：P <- (P + P^T) / 2。"""
    return 0.5 * (P + P.T)


def enforce_psd(P: np.ndarray, floor: float = PSD_FLOOR) -> tuple[np.ndarray, float]:
    """特征值分解，将所有特征值裁剪到 floor 以上，返回 (修复后矩阵, 最小原始特征值)。"""
    P = symmetrize(P)
    eigvals, eigvecs = np.linalg.eigh(P)
    min_eig = float(np.min(eigvals))
    clipped = np.maximum(eigvals, floor)
    P_fixed = (eigvecs * clipped) @ eigvecs.T
    return symmetrize(P_fixed), min_eig


def is_symmetric(R: np.ndarray, tol: float) -> bool:
    scale = float(np.max(np.abs(R))) if R.size else 0.0
    bound = tol * max(1.0, scale)
    return bool(np.all(np.abs(R - R.T) <= bound))


def is_psd(R: np.ndarray, tol: float) -> tuple[bool, float]:
    """对称矩阵通过特征值判断半正定；返回 (是否PSD, 最小特征值)。"""
    eigvals = np.linalg.eigvalsh(symmetrize(R))
    min_eig = float(np.min(eigvals))
    scale = float(np.max(np.abs(eigvals))) if eigvals.size else 0.0
    bound = tol * max(1.0, scale)
    return min_eig >= -bound, min_eig


def transition(dt: float) -> np.ndarray:
    F = np.eye(STATE_DIM, dtype=np.float64)
    F[0, 2] = dt
    F[1, 3] = dt
    return F


def process_noise(dt: float, q: float) -> np.ndarray:
    """连续白噪声加速度模型的离散过程噪声。"""
    dt = max(float(dt), 0.0)
    dt2 = dt * dt
    dt3 = dt2 * dt / 2.0
    dt4 = dt2 * dt2 / 4.0
    Q = np.zeros((STATE_DIM, STATE_DIM), dtype=np.float64)
    Q[0, 0] = Q[1, 1] = q * dt4
    Q[0, 2] = Q[2, 0] = q * dt3
    Q[1, 3] = Q[3, 1] = q * dt3
    Q[2, 2] = Q[3, 3] = q * dt2
    return Q


def predict(
    x: np.ndarray, P: np.ndarray, dt: float, q: float
) -> tuple[np.ndarray, np.ndarray]:
    """恒速预测；dt <= 0 时直接返回（已强制 PSD）。"""
    if dt <= 0.0:
        P_fixed, _ = enforce_psd(P)
        return x.copy(), P_fixed
    F = transition(dt)
    Q = process_noise(dt, q)
    x_new = F @ x
    P_new = F @ P @ F.T + Q
    P_new, _ = enforce_psd(P_new)
    return x_new, P_new


def _inverse_symmetric2x2(S: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """通过特征分解稳定求逆，同时返回用于 NIS 的 S_inv（特征值设下限）。"""
    eigvals, eigvecs = np.linalg.eigh(symmetrize(S))
    eigvals = np.maximum(eigvals, PSD_FLOOR)
    S_fixed = (eigvecs * eigvals) @ eigvecs.T
    S_inv = (eigvecs * (1.0 / eigvals)) @ eigvecs.T
    return symmetrize(S_fixed), symmetrize(S_inv)


def update(
    x: np.ndarray,
    P: np.ndarray,
    z: np.ndarray,
    R: np.ndarray,
    H: np.ndarray,
) -> dict:
    """Joseph 形式稳定更新，返回后验与完整创新量证据。

    返回字段：
      x, P, innovation, S, S_inv, K, nis, pre_min_eig, post_min_eig
    更新后的 P 经过对称化 + PSD 裁剪。
    """
    z = np.asarray(z, dtype=np.float64).reshape(MEAS_DIM)
    R = np.asarray(R, dtype=np.float64).reshape(MEAS_DIM, MEAS_DIM)

    innovation = z - H @ x
    S = H @ P @ H.T + R
    S, S_inv = _inverse_symmetric2x2(S)
    K = P @ H.T @ S_inv

    x_post = x + K @ innovation
    IKH = I4 - K @ H
    # Joseph 协方差更新：对结构误差最稳健，天然趋向对称 PSD
    P_post = IKH @ P @ IKH.T + K @ R @ K.T
    pre_min_eig = float(np.min(np.linalg.eigvalsh(symmetrize(P_post))))
    P_post, post_min_eig = enforce_psd(P_post)

    nis = float(innovation @ S_inv @ innovation)
    return {
        "x": x_post,
        "P": P_post,
        "innovation": innovation,
        "S": S,
        "S_inv": S_inv,
        "K": K,
        "nis": nis,
        "pre_min_eig": pre_min_eig,
        "post_min_eig": post_min_eig,
    }
