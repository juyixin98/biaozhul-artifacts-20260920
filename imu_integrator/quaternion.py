"""单位标量四元数工具（Hamilton 约定，[w, x, y, z]）。

所有旋转都遵循"先出现的样本先积分"的时间顺序。四元数 q 表示
*载体机体系 -> 导航/世界系* 的旋转：v_world = q * v_body * q^{-1}。
"""

from __future__ import annotations

import numpy as np


def quat_normalize(q: np.ndarray | list[float]) -> np.ndarray:
    """返回单位四元数；零模长时抛出 ValueError。"""
    q = np.asarray(q, dtype=float)
    norm = np.linalg.norm(q)
    if norm < 1e-15:
        raise ValueError("无法归一化零模长四元数")
    return q / norm


def quat_conjugate(q: np.ndarray) -> np.ndarray:
    """四元数共轭（单位四元数的逆）。"""
    q = np.asarray(q, dtype=float)
    return np.array([q[0], -q[1], -q[2], -q[3]])


def quat_multiply(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Hamilton 乘积。若 a、b 均为单位四元数则结果自动归一化返回。

    若 q = a * b 表示"先 b 后 a"的复合旋转。
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    q = np.array([
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
    ])
    # 组合旋转时定期归一化能抑制浮点漂移
    if abs(np.linalg.norm(a) - 1.0) < 1e-9 and abs(np.linalg.norm(b) - 1.0) < 1e-9:
        q = q / np.linalg.norm(q)
    return q


def quat_from_angle_axis(angle: float, axis: np.ndarray | list[float]) -> np.ndarray:
    """绕给定轴旋转 angle 弧度的四元数。"""
    axis = np.asarray(axis, dtype=float)
    axis_norm = np.linalg.norm(axis)
    if axis_norm < 1e-15:
        if abs(angle) < 1e-15:
            return np.array([1.0, 0.0, 0.0, 0.0])
        raise ValueError("旋转轴不能为零向量")
    axis = axis / axis_norm
    half = 0.5 * float(angle)
    s = np.sin(half)
    return np.array([np.cos(half), axis[0] * s, axis[1] * s, axis[2] * s])


def quat_rotate(q: np.ndarray, v: np.ndarray) -> np.ndarray:
    """用四元数旋转向量：v_world = q * [0,v] * q*。"""
    q = np.asarray(q, dtype=float)
    v = np.asarray(v, dtype=float)
    w, x, y, z = q
    vx, vy, vz = v
    # 展开式 q v q*，比构造纯四元数乘积略快
    return np.array([
        (1 - 2 * (y * y + z * z)) * vx + 2 * (x * y - w * z) * vy
        + 2 * (x * z + w * y) * vz,
        2 * (x * y + w * z) * vx + (1 - 2 * (x * x + z * z)) * vy
        + 2 * (y * z - w * x) * vz,
        2 * (x * z - w * y) * vx + 2 * (y * z + w * x) * vy
        + (1 - 2 * (x * x + y * y)) * vz,
    ])


def quat_to_rotation_matrix(q: np.ndarray) -> np.ndarray:
    """返回对应的 3x3 旋转矩阵（机体系 -> 世界系）。"""
    q = quat_normalize(np.asarray(q, dtype=float))
    w, x, y, z = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ])
