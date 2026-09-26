"""刚体变换（SE(3)）：平移 + 单位四元数旋转。

约定：Transform(translation=t, rotation=q) 表示 "child 在 parent 中的位姿"，
即把 child 坐标系下的点 p_child 映射到 parent 坐标系：

    p_parent = R(q) @ p_child + t
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import quaternion as quat


@dataclass(frozen=True)
class Transform:
    """不可变刚体变换。translation: (3,)，rotation: (4,) 单位四元数 (x,y,z,w)。"""

    translation: np.ndarray
    rotation: np.ndarray

    def __post_init__(self) -> None:
        t = np.asarray(self.translation, dtype=float)
        if t.shape != (3,):
            raise ValueError(f"平移必须为长度 3 的向量，得到形状 {t.shape}")
        object.__setattr__(self, "translation", t)
        object.__setattr__(self, "rotation", quat.normalize(self.rotation))

    @staticmethod
    def identity() -> "Transform":
        return Transform(np.zeros(3), np.array([0.0, 0.0, 0.0, 1.0]))

    def inverse(self) -> "Transform":
        """逆变换：parent_T_child 的逆是 child_T_parent。"""
        r_inv = quat.conjugate(self.rotation)
        t_inv = -quat.rotate(r_inv, self.translation)
        return Transform(t_inv, r_inv)

    def compose(self, other: "Transform") -> "Transform":
        """self ∘ other：先施加 other，再施加 self。

        若 self = a_T_b、other = b_T_c，则结果为 a_T_c。
        """
        r = quat.multiply(self.rotation, other.rotation)
        t = quat.rotate(self.rotation, other.translation) + self.translation
        return Transform(t, r)

    def apply(self, point: np.ndarray) -> np.ndarray:
        """把点从子坐标系映射到父坐标系。"""
        return quat.rotate(self.rotation, np.asarray(point, dtype=float)) + self.translation

    def as_matrix(self) -> np.ndarray:
        """返回 4x4 齐次变换矩阵。"""
        m = np.eye(4)
        m[:3, :3] = quat.to_matrix(self.rotation)
        m[:3, 3] = self.translation
        return m
