"""SE3 刚体变换：平移 + 单位四元数表示的旋转。

四元数约定为 ``[x, y, z, w]``（与 SciPy ``Rotation`` 一致）。
旋转插值使用手写 SLERP（含反号等价处理），四元数 <-> 旋转矩阵的转换交给
SciPy，避免手写旋转公式出错。
"""

from __future__ import annotations

import numpy as np
from scipy.spatial.transform import Rotation

from .errors import InvalidTransformError

# 判定为单位四元数的容差：偏差在该范围内自动归一化，超出则拒绝。
_UNIT_QUAT_TOL = 1e-4
# SLERP 夹角小于该阈值（cos 接近 1）时退化为归一化线性插值。
_SLERP_CODOMAIN_THRESHOLD = 0.9995

Quat = np.ndarray  # shape (4,), [x, y, z, w]
Vec3 = np.ndarray  # shape (3,)


def _as_vec3(v, name: str) -> Vec3:
    arr = np.asarray(v, dtype=float)
    if arr.shape != (3,):
        raise InvalidTransformError(f"{name} 必须是长度为 3 的向量，实际形状 {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise InvalidTransformError(f"{name} 含有非有限值（NaN/Inf）")
    return arr


def _as_unit_quat(q, name: str = "quaternion") -> Quat:
    arr = np.asarray(q, dtype=float)
    if arr.shape != (4,):
        raise InvalidTransformError(f"{name} 必须是长度为 4 的 [x,y,z,w]，实际形状 {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise InvalidTransformError(f"{name} 含有非有限值（NaN/Inf）")
    norm = float(np.linalg.norm(arr))
    if norm == 0.0:
        raise InvalidTransformError(f"{name} 是零向量，无法表示旋转")
    if abs(norm - 1.0) > _UNIT_QUAT_TOL:
        raise InvalidTransformError(
            f"{name} 不是单位四元数（|q|={norm:.6g}，容差 {_UNIT_QUAT_TOL:g}）"
        )
    return arr / norm


def slerp(q0: Quat, q1: Quat, alpha: float) -> Quat:
    """两个单位四元数之间的球面线性插值。

    参数:
        q0, q1: 单位四元数 [x,y,z,w]。
        alpha: 插值系数，0 返回 q0，1 返回 q1。

    正确处理四元数反号等价：q 与 -q 表示同一旋转，插值前会把 q1 翻转到
    与 q0 点积非负的半球，保证走短弧且路径连续。
    """
    q0 = _as_unit_quat(q0, "q0")
    q1 = _as_unit_quat(q1, "q1")
    dot = float(np.dot(q0, q1))
    if dot < 0.0:
        # 反号等价：走短弧
        q1 = -q1
        dot = -dot
    dot = min(1.0, max(-1.0, dot))

    if dot > _SLERP_CODOMAIN_THRESHOLD:
        # 夹角极小，SLERP 分母趋近 0，退化为归一化线性插值
        q = (1.0 - alpha) * q0 + alpha * q1
        return q / np.linalg.norm(q)

    theta_0 = np.arccos(dot)
    theta = theta_0 * alpha
    sin_theta_0 = np.sin(theta_0)
    q = q0 * (np.sin(theta_0 - theta) / sin_theta_0) + q1 * (
        np.sin(theta) / sin_theta_0
    )
    return q / np.linalg.norm(q)


class SE3Transform:
    """三维刚体变换 ``p' = R @ p + t``。

    属性:
        translation: shape (3,) 平移向量。
        quaternion:  shape (4,) 单位四元数 [x,y,z,w]。
    """

    __slots__ = ("translation", "quaternion")

    def __init__(self, translation, quaternion):
        self.translation = _as_vec3(translation, "translation")
        self.quaternion = _as_unit_quat(quaternion)

    # ---- 构造 ----------------------------------------------------------
    @classmethod
    def identity(cls) -> "SE3Transform":
        return cls(np.zeros(3), np.array([0.0, 0.0, 0.0, 1.0]))

    @classmethod
    def from_matrix(cls, matrix) -> "SE3Transform":
        m = np.asarray(matrix, dtype=float)
        if m.shape != (4, 4):
            raise InvalidTransformError(f"矩阵必须是 4x4，实际形状 {m.shape}")
        if not np.all(np.isfinite(m)):
            raise InvalidTransformError("矩阵含有非有限值（NaN/Inf）")
        rot = Rotation.from_matrix(m[:3, :3])
        return cls(m[:3, 3].copy(), rot.as_quat())

    # ---- 导出 ----------------------------------------------------------
    def rotation_matrix(self) -> np.ndarray:
        return Rotation.from_quat(self.quaternion).as_matrix()

    def to_matrix(self) -> np.ndarray:
        m = np.eye(4)
        m[:3, :3] = self.rotation_matrix()
        m[:3, 3] = self.translation
        return m

    def to_dict(self) -> dict:
        return {
            "translation": self.translation.tolist(),
            "quaternion": self.quaternion.tolist(),
        }

    # ---- 群运算 --------------------------------------------------------
    def multiply(self, other: "SE3Transform") -> "SE3Transform":
        """返回 self ∘ other：先作用 other，再作用 self。"""
        r = Rotation.from_quat(self.quaternion) * Rotation.from_quat(other.quaternion)
        t = self.translation + self.rotation_matrix() @ other.translation
        return SE3Transform(t, r.as_quat())

    def inverse(self) -> "SE3Transform":
        """返回逆变换 T^{-1}。"""
        r_inv = Rotation.from_quat(self.quaternion).inv()
        q_inv = r_inv.as_quat()
        t_inv = -r_inv.as_matrix() @ self.translation
        return SE3Transform(t_inv, q_inv)

    def apply(self, points) -> np.ndarray:
        """把变换作用到一组点上，points shape (..., 3)。"""
        pts = np.asarray(points, dtype=float)
        return pts @ self.rotation_matrix().T + self.translation

    # ---- 插值 ----------------------------------------------------------
    @classmethod
    def interpolate(
        cls, a: "SE3Transform", b: "SE3Transform", alpha: float
    ) -> "SE3Transform":
        """平移线性插值，旋转 SLERP。alpha=0 取 a，alpha=1 取 b。"""
        if not 0.0 <= alpha <= 1.0:
            # 插值只定义在段内；段外属于外推，由上层拒绝
            raise InvalidTransformError(f"插值系数必须在 [0,1] 内，实际 {alpha}")
        t = (1.0 - alpha) * a.translation + alpha * b.translation
        q = slerp(a.quaternion, b.quaternion, alpha)
        return cls(t, q)

    # ---- 内部辅助 ------------------------------------------------------
    def copy(self) -> "SE3Transform":
        return SE3Transform(self.translation.copy(), self.quaternion.copy())

    def __matmul__(self, other) -> "SE3Transform":
        if not isinstance(other, SE3Transform):
            return NotImplemented
        return self.multiply(other)

    def __eq__(self, other) -> bool:
        if not isinstance(other, SE3Transform):
            return NotImplemented
        # 比较旋转矩阵而非四元数：q 与 -q 表示同一旋转
        return np.allclose(self.translation, other.translation) and np.allclose(
            self.rotation_matrix(), other.rotation_matrix()
        )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"SE3Transform(t={self.translation.tolist()}, "
            f"q=[x={self.quaternion[0]:.4f}, y={self.quaternion[1]:.4f}, "
            f"z={self.quaternion[2]:.4f}, w={self.quaternion[3]:.4f}])"
        )
