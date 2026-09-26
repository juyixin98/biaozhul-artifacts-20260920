"""刚体变换：旋转矩阵 + 平移向量，齐次矩阵表示为 T = [[R, t], [0, 1]]。

约定：T_parent_child 把点在 child 中的坐标变换到 parent：
    p_parent = R @ p_child + t
"""

from __future__ import annotations

import numpy as np

from .errors import InvalidTransformError

_ROT_TOL = 1e-9
# 正交归一化时，与正交矩阵的最大允许偏差；超过则视为非法旋转。
_ORTHONORMAL_TOL = 1e-6


class Transform:
    """不可变的刚体变换（R, t）。"""

    __slots__ = ("_rotation", "_translation")

    def __init__(self, rotation: np.ndarray | list | None, translation: np.ndarray | list):
        r = np.asarray(rotation, dtype=float) if rotation is not None else np.eye(3)
        t = np.asarray(translation, dtype=float).reshape(3)
        if r.shape != (3, 3):
            raise InvalidTransformError(f"旋转矩阵必须是 3x3，实际形状 {r.shape}")
        if not np.all(np.isfinite(r)) or not np.all(np.isfinite(t)):
            raise InvalidTransformError("旋转矩阵和平移向量必须全部为有限数值")
        self._rotation = self._validate_rotation(r)
        self._translation = t

    @staticmethod
    def _validate_rotation(r: np.ndarray) -> np.ndarray:
        """校验旋转矩阵（正交且行列式为 +1），返回拷贝。"""
        should_be_identity = r.T @ r
        if not np.allclose(should_be_identity, np.eye(3), atol=_ORTHONORMAL_TOL):
            raise InvalidTransformError("旋转矩阵不是正交矩阵（R^T R != I）")
        det = float(np.linalg.det(r))
        if not np.isclose(det, 1.0, atol=_ORTHONORMAL_TOL):
            raise InvalidTransformError(f"旋转矩阵行列式必须为 +1，实际为 {det:.6f}（疑似反射）")
        # 通过校验后做一次精正交化，消除浮点误差。
        u, _, vt = np.linalg.svd(r)
        return u @ vt

    @property
    def rotation(self) -> np.ndarray:
        return self._rotation

    @property
    def translation(self) -> np.ndarray:
        return self._translation

    @classmethod
    def identity(cls) -> "Transform":
        return cls(np.eye(3), np.zeros(3))

    @classmethod
    def from_quaternion(cls, q: np.ndarray | list, translation: np.ndarray | list = (0, 0, 0)) -> "Transform":
        """从单位四元数 [w, x, y, z] 构造。"""
        q = np.asarray(q, dtype=float).reshape(4)
        if not np.all(np.isfinite(q)):
            raise InvalidTransformError("四元数必须全部为有限数值")
        norm = float(np.linalg.norm(q))
        if norm < 1e-12:
            raise InvalidTransformError("四元数不能为零向量")
        q = q / norm
        w, x, y, z = q
        r = np.array(
            [
                [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
            ],
            dtype=float,
        )
        out = cls.__new__(cls)
        out._rotation = r
        out._translation = np.asarray(translation, dtype=float).reshape(3).copy()
        if not np.all(np.isfinite(out._translation)):
            raise InvalidTransformError("平移向量必须全部为有限数值")
        return out

    @classmethod
    def from_matrix(cls, matrix: np.ndarray | list) -> "Transform":
        m = np.asarray(matrix, dtype=float)
        if m.shape != (4, 4):
            raise InvalidTransformError(f"齐次矩阵必须是 4x4，实际形状 {m.shape}")
        if not np.all(np.isfinite(m)):
            raise InvalidTransformError("齐次矩阵必须全部为有限数值")
        if not np.allclose(m[3, :], [0, 0, 0, 1], atol=1e-9):
            raise InvalidTransformError("齐次矩阵最后一行必须是 [0, 0, 0, 1]")
        return cls(m[:3, :3], m[:3, 3])

    def to_matrix(self) -> np.ndarray:
        m = np.eye(4)
        m[:3, :3] = self._rotation
        m[:3, 3] = self._translation
        return m

    def to_quaternion(self) -> np.ndarray:
        """旋转矩阵 -> 四元数 [w, x, y, z]，w >= 0。"""
        return _matrix_to_quaternion(self._rotation)

    def multiply(self, other: "Transform") -> "Transform":
        """返回 self * other：先做 other 再做 self。"""
        return Transform(
            self._rotation @ other._rotation,
            self._rotation @ other._translation + self._translation,
        )

    def inverse(self) -> "Transform":
        """返回逆变换 T^{-1}（R^T, -R^T t）。"""
        r_inv = self._rotation.T
        return Transform(r_inv, -(r_inv @ self._translation))

    def transform_point(self, point: np.ndarray | list) -> np.ndarray:
        return self._rotation @ np.asarray(point, dtype=float).reshape(3) + self._translation

    def is_close(self, other: "Transform", atol: float = 1e-9) -> bool:
        return np.allclose(self._rotation, other._rotation, atol=atol) and np.allclose(
            self._translation, other._translation, atol=atol
        )

    def __matmul__(self, other: "Transform") -> "Transform":
        return self.multiply(other)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Transform):
            return NotImplemented
        return self.is_close(other)

    def __repr__(self) -> str:
        return f"Transform(t={self._translation.tolist()})"


def quaternion_slerp(q0: np.ndarray, q1: np.ndarray, alpha: float) -> np.ndarray:
    """单位四元数球面线性插值，走最短弧（dot < 0 时翻转 q1），返回 [w,x,y,z]。

    alpha=0 -> q0，alpha=1 -> q1。输入无需预先归一化（内部归一化）。
    """
    q0 = np.asarray(q0, dtype=float).reshape(4)
    q1 = np.asarray(q1, dtype=float).reshape(4)
    q0 = q0 / np.linalg.norm(q0)
    q1 = q1 / np.linalg.norm(q1)
    dot = float(np.clip(np.dot(q0, q1), -1.0, 1.0))
    if dot < 0.0:
        q1 = -q1
        dot = -dot
    # 夹角很小时退化为归一化线性插值，避免 1/sin(theta) 数值爆炸。
    if dot > 0.9995:
        q = q0 + alpha * (q1 - q0)
        return q / np.linalg.norm(q)
    theta = np.arccos(dot)
    sin_theta = np.sin(theta)
    w0 = np.sin((1.0 - alpha) * theta) / sin_theta
    w1 = np.sin(alpha * theta) / sin_theta
    return w0 * q0 + w1 * q1


def _matrix_to_quaternion(r: np.ndarray) -> np.ndarray:
    """旋转矩阵 -> 单位四元数 [w, x, y, z]，保证 w >= 0。Shepperd 方法，数值稳定。"""
    trace = float(np.trace(r))
    if trace > 0.0:
        s = 2.0 * np.sqrt(trace + 1.0)
        q = np.array([0.25 * s, (r[2, 1] - r[1, 2]) / s, (r[0, 2] - r[2, 0]) / s, (r[1, 0] - r[0, 1]) / s])
    elif r[0, 0] > r[1, 1] and r[0, 0] > r[2, 2]:
        s = 2.0 * np.sqrt(1.0 + r[0, 0] - r[1, 1] - r[2, 2])
        q = np.array([(r[2, 1] - r[1, 2]) / s, 0.25 * s, (r[0, 1] + r[1, 0]) / s, (r[0, 2] + r[2, 0]) / s])
    elif r[1, 1] > r[2, 2]:
        s = 2.0 * np.sqrt(1.0 + r[1, 1] - r[0, 0] - r[2, 2])
        q = np.array([(r[0, 2] - r[2, 0]) / s, (r[0, 1] + r[1, 0]) / s, 0.25 * s, (r[1, 2] + r[2, 1]) / s])
    else:
        s = 2.0 * np.sqrt(1.0 + r[2, 2] - r[0, 0] - r[1, 1])
        q = np.array([(r[1, 0] - r[0, 1]) / s, (r[0, 2] + r[2, 0]) / s, (r[1, 2] + r[2, 1]) / s, 0.25 * s])
    q = q / np.linalg.norm(q)
    if q[0] < 0.0:
        q = -q
    return q
