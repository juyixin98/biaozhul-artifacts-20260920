"""单位四元数运算，约定分量顺序为 (x, y, z, w)。

旋转采用主动变换约定：向量 v 经四元数 q 旋转得到 q * v * q^{-1}。
"""

from __future__ import annotations

import numpy as np

_QUAT_NORM_EPS = 1e-12


def normalize(q: np.ndarray) -> np.ndarray:
    """归一化四元数；模长过小视为非法输入。"""
    q = np.asarray(q, dtype=float)
    if q.shape != (4,):
        raise ValueError(f"四元数必须为长度为 4 的向量，得到形状 {q.shape}")
    norm = np.linalg.norm(q)
    if norm < _QUAT_NORM_EPS:
        raise ValueError("四元数模长过小，无法归一化")
    return q / norm


def multiply(q1: np.ndarray, q2: np.ndarray) -> np.ndarray:
    """四元数乘法 q1 * q2（Hamilton 积），表示先施加 q2 再施加 q1。"""
    x1, y1, z1, w1 = q1
    x2, y2, z2, w2 = q2
    return np.array(
        [
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        ]
    )


def conjugate(q: np.ndarray) -> np.ndarray:
    """共轭四元数；对单位四元数即逆旋转。"""
    return np.array([-q[0], -q[1], -q[2], q[3]])


def rotate(q: np.ndarray, v: np.ndarray) -> np.ndarray:
    """用四元数 q 旋转向量 v。"""
    qv = np.array([v[0], v[1], v[2], 0.0])
    return multiply(multiply(q, qv), conjugate(q))[:3]


def slerp(q0: np.ndarray, q1: np.ndarray, u: float) -> np.ndarray:
    """球面线性插值，u ∈ [0, 1]；自动处理符号歧义与退化情形。"""
    q0 = normalize(q0)
    q1 = normalize(q1)
    dot = float(np.dot(q0, q1))
    if dot < 0.0:  # 取较短弧
        q1 = -q1
        dot = -dot
    if dot > 0.9995:  # 夹角过小退化为线性插值，避免除零
        return normalize(q0 + u * (q1 - q0))
    theta = float(np.arccos(np.clip(dot, -1.0, 1.0)))
    sin_theta = np.sin(theta)
    a = np.sin((1.0 - u) * theta) / sin_theta
    b = np.sin(u * theta) / sin_theta
    return normalize(a * q0 + b * q1)


def to_matrix(q: np.ndarray) -> np.ndarray:
    """单位四元数转 3x3 旋转矩阵。"""
    x, y, z, w = normalize(q)
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def from_axis_angle(axis: np.ndarray, angle: float) -> np.ndarray:
    """由旋转轴与弧度角构造四元数，便于测试与示例构造数据。"""
    axis = np.asarray(axis, dtype=float)
    norm = np.linalg.norm(axis)
    if norm < _QUAT_NORM_EPS:
        raise ValueError("旋转轴模长过小")
    axis = axis / norm
    half = angle / 2.0
    return np.array([*(axis * np.sin(half)), np.cos(half)])
