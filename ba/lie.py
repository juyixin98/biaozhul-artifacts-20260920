"""SO(3) 李群/李代数工具（Rodrigues 公式）。"""

from __future__ import annotations

import numpy as np


def skew(v: np.ndarray) -> np.ndarray:
    """三维向量的反对称矩阵 [v]_x。"""
    v = np.asarray(v, dtype=float).reshape(3)
    return np.array(
        [
            [0.0, -v[2], v[1]],
            [v[2], 0.0, -v[0]],
            [-v[1], v[0], 0.0],
        ]
    )


def exp_so3(w: np.ndarray) -> np.ndarray:
    """旋转向量 -> 旋转矩阵（Rodrigues 公式）。"""
    w = np.asarray(w, dtype=float).reshape(3)
    theta = float(np.linalg.norm(w))
    W = skew(w)
    if theta < 1e-12:
        # 二阶泰勒展开，避免小角度数值问题
        return np.eye(3) + W + 0.5 * (W @ W)
    a = np.sin(theta) / theta
    b = (1.0 - np.cos(theta)) / (theta * theta)
    return np.eye(3) + a * W + b * (W @ W)


def log_so3(R: np.ndarray) -> np.ndarray:
    """旋转矩阵 -> 旋转向量。"""
    R = np.asarray(R, dtype=float).reshape(3, 3)
    cos_theta = float(np.clip((np.trace(R) - 1.0) / 2.0, -1.0, 1.0))
    theta = float(np.arccos(cos_theta))
    if theta < 1e-12:
        return np.zeros(3)
    if abs(np.pi - theta) < 1e-6:
        # 接近 180 度：从对称部分恢复旋转轴
        A = (R + np.eye(3)) / 2.0
        axis = np.sqrt(np.clip(np.diag(A), 0.0, None))
        # 用非对角元确定符号
        if R[2, 1] - R[1, 2] < 0:
            axis[0] = -axis[0]
        if R[0, 2] - R[2, 0] < 0:
            axis[1] = -axis[1]
        if R[1, 0] - R[0, 1] < 0:
            axis[2] = -axis[2]
        n = np.linalg.norm(axis)
        if n < 1e-12:
            return np.array([np.pi, 0.0, 0.0])
        return theta * axis / n
    w = np.array(
        [R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]]
    )
    return theta / (2.0 * np.sin(theta)) * w
