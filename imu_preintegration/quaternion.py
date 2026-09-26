"""四元数工具函数。

约定：
- 四元数格式为 [w, x, y, z]，单位四元数表示机体坐标系到世界坐标系的旋转。
- 旋转矩阵 R 满足 v_world = R @ v_body。
"""

from __future__ import annotations

import numpy as np


def normalize(q: np.ndarray) -> np.ndarray:
    """返回单位化后的四元数副本；零四元数抛出 ValueError。"""
    q = np.asarray(q, dtype=float)
    norm = np.linalg.norm(q)
    if norm == 0.0:
        raise ValueError("cannot normalize a zero quaternion")
    return q / norm


def multiply(q1: np.ndarray, q2: np.ndarray) -> np.ndarray:
    """四元数乘法 q1 ⊗ q2（先施加 q2 的旋转，再施加 q1）。"""
    w1, x1, y1, z1 = q1
    w2, x2, y2, z2 = q2
    return np.array(
        [
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        ],
        dtype=float,
    )


def from_rotvec(rotvec: np.ndarray) -> np.ndarray:
    """由旋转向量（轴角，弧度）构造单位四元数。"""
    rotvec = np.asarray(rotvec, dtype=float)
    angle = float(np.linalg.norm(rotvec))
    if angle < 1e-12:
        # 小角度近似，避免除零
        half = 0.5 * rotvec
        return normalize(np.array([1.0, half[0], half[1], half[2]]))
    axis = rotvec / angle
    half = 0.5 * angle
    s = np.sin(half)
    return np.array([np.cos(half), s * axis[0], s * axis[1], s * axis[2]])


def to_matrix(q: np.ndarray) -> np.ndarray:
    """单位四元数转 3x3 旋转矩阵（机体系 -> 世界系）。"""
    w, x, y, z = normalize(q)
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=float,
    )


def rotate(q: np.ndarray, v: np.ndarray) -> np.ndarray:
    """用四元数旋转向量：v_world = R(q) @ v_body。"""
    return to_matrix(q) @ np.asarray(v, dtype=float)


def conjugate(q: np.ndarray) -> np.ndarray:
    """四元数共轭（单位四元数即逆旋转）。"""
    q = np.asarray(q, dtype=float)
    return np.array([q[0], -q[1], -q[2], -q[3]], dtype=float)
