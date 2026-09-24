"""SE(3) 李群/李代数运算（纯 NumPy 实现）。

扰动向量约定为 6 维 ``xi = [v; omega]``：前 3 维平移、后 3 维旋转（轴角），
对应齐次矩阵::

    xi^ = [ omega^  v ]
          [  0      0 ]

支持两种扰动约定：

* 右扰动 (right)： ``T' = T * Exp(xi)``
* 左扰动 (left) ： ``T' = Exp(xi) * T``

协方差矩阵 6x6 的分块顺序同样为 ``[平移, 旋转]``。
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "skew",
    "so3_exp",
    "so3_log",
    "se3_exp",
    "se3_log",
    "adjoint",
    "inv_tf",
    "quat_to_rot",
    "rot_to_quat",
    "make_tf",
]

# SO(3) 对合区域附近的数值阈值
_SMALL_ANGLE = 1e-8
_PI = float(np.pi)


def skew(v: np.ndarray) -> np.ndarray:
    """3 维向量 -> 3x3 反对称矩阵。"""
    v = np.asarray(v, dtype=float).reshape(3)
    return np.array(
        [
            [0.0, -v[2], v[1]],
            [v[2], 0.0, -v[0]],
            [-v[1], v[0], 0.0],
        ]
    )


def so3_exp(phi: np.ndarray) -> np.ndarray:
    """Rodrigues 公式：旋转向量 -> 旋转矩阵。"""
    phi = np.asarray(phi, dtype=float).reshape(3)
    angle = float(np.linalg.norm(phi))
    if angle < _SMALL_ANGLE:
        # 二阶 Taylor 展开，避免除零
        K = skew(phi)
        return np.eye(3) + K + 0.5 * K @ K
    K = skew(phi / angle)
    s, c = np.sin(angle), np.cos(angle)
    return np.eye(3) + s * K + (1.0 - c) * K @ K


def so3_log(R: np.ndarray) -> np.ndarray:
    """旋转矩阵 -> 旋转向量（|angle| <= pi）。

    全程避免在 ±1 处直接 ``arccos``：theta<pi/2 用 ``asin|sin|``，
    theta>=pi/2 用 ``pi-asin|sin|``；轴方向统一 ``v/sin(theta)``（小
    delta 时 sin(delta)≈delta 极精确），仅 theta 恰为 pi 时从
    ``R + I = 2 n n^T`` 提轴。

    数值边界：theta = pi - delta（1e-7 量级）存在固有不可区分性——
    ``pi - asin(delta)`` 的 O(eps) 角度误差经单位轴放大，旋转矩阵重构误差
    上界约 2e-9；delta -> 0（精确 pi）时 +/-pi*n 等价，误差回落。
    这不是实现缺陷：两个相差近 2pi 的大角度绕近轴旋转本就难以区分，
    闭环审计应同时看平移残差与整体 Mahalanobis 距离。
    """
    R = np.asarray(R, dtype=float).reshape(3, 3)
    # vee(R - R^T)/2 = sin(theta) n
    v = 0.5 * np.array(
        [R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]]
    )
    s = float(np.linalg.norm(v))  # |sin(theta)|（未截断，用于小角度）
    c = float(np.clip((np.trace(R) - 1.0) / 2.0, -1.0, 1.0))

    if s < 1e-4 and c > 0:
        # 小角度：log(R) = theta n ~= sin(theta) n = v，误差 O(theta^3)
        return v

    # 角度：近 0 用 asin|sin|（良态），近 pi 用 pi-asin|sin|（规避 arccos 病态）
    s_clip = min(max(s, 0.0), 1.0)
    if c > 0:
        angle = float(np.arcsin(s_clip))  # theta < pi/2
    elif s < 1e-12:
        # 几乎恰为 pi（sin theta 数值为 0）：+/-pi*n 等价，
        # 从 R + I = 2 n n^T 提轴
        S = R + np.eye(3)
        col = int(np.argmax(np.linalg.norm(S, axis=0)))
        return _PI * S[:, col] / np.linalg.norm(S[:, col])
    else:
        angle = _PI - float(np.arcsin(s_clip))  # theta >= pi/2

    # 转轴：theta n = (theta/sin theta) v。近 pi 时 sin theta = sin(delta)
    # 对小 delta 极精确（asin 良态），故全区间统一用 v/s，无需 R+I 提轴。
    axis = v / s
    return angle * axis


def _vee_so3(M: np.ndarray) -> np.ndarray:
    return 0.5 * np.array(
        [M[2, 1] - M[1, 2], M[0, 2] - M[2, 0], M[1, 0] - M[0, 1]]
    )


def se3_exp(xi: np.ndarray) -> np.ndarray:
    """6 维扰动向量 -> 4x4 齐次变换。"""
    xi = np.asarray(xi, dtype=float).reshape(6)
    v, phi = xi[:3], xi[3:]
    angle = float(np.linalg.norm(phi))
    R = so3_exp(phi)
    if angle < _SMALL_ANGLE:
        Kt = skew(phi)
        # V = I + [φ]/2 + [φ]²/6
        V = np.eye(3) + 0.5 * Kt + (1.0 / 6.0) * (Kt @ Kt)
    else:
        Kt = skew(phi)  # [φ]，非单位轴
        # V = I + ((1-cosθ)/θ²) [φ] + ((θ-sinθ)/θ³) [φ]²
        V = (
            np.eye(3)
            + ((1.0 - np.cos(angle)) / angle**2) * Kt
            + ((angle - np.sin(angle)) / angle**3) * (Kt @ Kt)
        )
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = V @ v
    return T


def se3_log(T: np.ndarray) -> np.ndarray:
    """4x4 齐次变换 -> 6 维向量 [v; omega]（局部坐标）。"""
    T = np.asarray(T, dtype=float).reshape(4, 4)
    R, t = T[:3, :3], T[:3, 3]
    phi = so3_log(R)
    angle = float(np.linalg.norm(phi))
    Kt = skew(phi)  # [φ]
    if angle < 1e-4:
        # Taylor，θ<1e-4 时截断误差 O(θ^4) < 1e-16
        # V^-1 = I - [φ]/2 + [φ]²/12
        V_inv = np.eye(3) - 0.5 * Kt + (1.0 / 12.0) * (Kt @ Kt)
    else:
        K = skew(phi / angle)  # 单位反对称轴
        # V^-1 = I - [φ]/2 + (b/θ²)[φ]²，
        # b = 1 - (θ/2) cot(θ/2)，用 sinc 稳定计算（b∈[0,~1]，无相消）：
        #   cot(θ/2) = cos(θ/2)/sinc(θ/2)·(2/θ)
        half = 0.5 * angle
        sinc = np.sin(half) / half
        b = 1.0 - np.cos(half) / sinc
        cb = b / angle**2
        V_inv = np.eye(3) - 0.5 * Kt + cb * (Kt @ Kt)
    v = V_inv @ t
    return np.concatenate([v, phi])


def adjoint(T: np.ndarray) -> np.ndarray:
    """伴随矩阵 Ad_T（6 维向量序 [v; omega]）。"""
    T = np.asarray(T, dtype=float).reshape(4, 4)
    R, t = T[:3, :3], T[:3, 3]
    Ad = np.zeros((6, 6))
    Ad[:3, :3] = R
    Ad[:3, 3:] = skew(t) @ R
    Ad[3:, 3:] = R
    return Ad


def inv_tf(T: np.ndarray) -> np.ndarray:
    """SE(3) 逆：[R t;0 1]^{-1} = [R^T -R^T t; 0 1]。"""
    T = np.asarray(T, dtype=float).reshape(4, 4)
    R, t = T[:3, :3], T[:3, 3]
    out = np.eye(4)
    out[:3, :3] = R.T
    out[:3, 3] = -R.T @ t
    return out


def quat_to_rot(q: np.ndarray) -> np.ndarray:
    """四元数 [w, x, y, z] -> 旋转矩阵（调用方负责合法性校验）。"""
    w, x, y, z = (float(v) for v in np.asarray(q, dtype=float).reshape(4))
    n = np.sqrt(w * w + x * x + y * y + z * z)
    w, x, y, z = w / n, x / n, y / n, z / n
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def rot_to_quat(R: np.ndarray) -> np.ndarray:
    """旋转矩阵 -> 四元数 [w, x, y, z]（Shepperd 方法，数值稳定）。"""
    R = np.asarray(R, dtype=float).reshape(3, 3)
    trace = np.trace(R)
    if trace > 0:
        s = 2.0 * np.sqrt(trace + 1.0)
        w = 0.25 * s
        x = (R[2, 1] - R[1, 2]) / s
        y = (R[0, 2] - R[2, 0]) / s
        z = (R[1, 0] - R[0, 1]) / s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = 2.0 * np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2])
        w = (R[2, 1] - R[1, 2]) / s
        x = 0.25 * s
        y = (R[0, 1] + R[1, 0]) / s
        z = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = 2.0 * np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2])
        w = (R[0, 2] - R[2, 0]) / s
        x = (R[0, 1] + R[1, 0]) / s
        y = 0.25 * s
        z = (R[1, 2] + R[2, 1]) / s
    else:
        s = 2.0 * np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1])
        w = (R[1, 0] - R[0, 1]) / s
        x = (R[0, 2] + R[2, 0]) / s
        y = (R[1, 2] + R[2, 1]) / s
        z = 0.25 * s
    q = np.array([w, x, y, z])
    if q[0] < 0:
        q = -q
    return q / np.linalg.norm(q)


def make_tf(translation: np.ndarray, rotation: np.ndarray) -> np.ndarray:
    """平移向量 + 3x3 旋转 -> 4x4 齐次矩阵。"""
    T = np.eye(4)
    T[:3, :3] = np.asarray(rotation, dtype=float).reshape(3, 3)
    T[:3, 3] = np.asarray(translation, dtype=float).reshape(3)
    return T
