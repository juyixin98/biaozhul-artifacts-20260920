"""机器人模型：DH 参数、关节限位、正运动学与数值雅可比。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..params_loader import PARAMS_PATH, load_params


@dataclass(frozen=True)
class RobotModel:
    name: str
    a: np.ndarray          # (6,)
    d: np.ndarray          # (6,)
    alpha: np.ndarray      # (6,)
    theta_offset: np.ndarray
    joint_lower: np.ndarray
    joint_upper: np.ndarray

    @classmethod
    def from_params(cls, params: dict | None = None) -> "RobotModel":
        p = params or load_params()
        dh = p["dh"]
        a = np.array([row["a"] for row in dh], dtype=float)
        d = np.array([row["d"] for row in dh], dtype=float)
        alpha = np.array([row["alpha"] for row in dh], dtype=float)
        offs = np.array([row["theta_offset"] for row in dh], dtype=float)
        lim = p["joint_limits"]
        return cls(
            name=p["robot"]["name"],
            a=a,
            d=d,
            alpha=alpha,
            theta_offset=offs,
            joint_lower=np.array(lim["lower"], dtype=float),
            joint_upper=np.array(lim["upper"], dtype=float),
        )

    @property
    def n_joints(self) -> int:
        return self.a.size

    def link_transform(self, q: np.ndarray, i: int) -> np.ndarray:
        """标准 DH：T_i = Rotz(theta) Transz(d) Transx(a) Rotx(alpha)。"""
        theta = self.theta_offset[i] + q[i]
        ct, st = np.cos(theta), np.sin(theta)
        ca, sa = np.cos(self.alpha[i]), np.sin(self.alpha[i])
        a, d = self.a[i], self.d[i]
        return np.array(
            [
                [ct, -st * ca, st * sa, a * ct],
                [st, ct * ca, -ct * sa, a * st],
                [0.0, sa, ca, d],
                [0.0, 0.0, 0.0, 1.0],
            ],
            dtype=float,
        )

    def fk_all(self, q: np.ndarray) -> list[np.ndarray]:
        """返回 T0_1 ... T0_n（基坐标下各连杆系位姿）。"""
        q = np.asarray(q, dtype=float)
        frames = []
        t = np.eye(4)
        for i in range(self.n_joints):
            t = t @ self.link_transform(q, i)
            frames.append(t)
        return frames

    def fk(self, q: np.ndarray) -> np.ndarray:
        """末端法兰 T0_n (4x4)。"""
        return self.fk_all(q)[-1]

    def position(self, q: np.ndarray) -> np.ndarray:
        return self.fk(q)[:3, 3]

    def numerical_jacobian(
        self, q: np.ndarray, T_des: np.ndarray | None = None, eps: float = 1e-6
    ) -> np.ndarray:
        """位姿残差对关节角的中心差分雅可比 (6 x n)。

        残差定义必须与 IK 中使用的完全一致：
            e(q) = [p_des - p(q); rotation_error(R(q), R_des)]
        因此差分直接对 e(q±eps·e_k) 进行——若参考姿态取错（例如取 I），
        远离目标时姿态三行就是错的，阻尼迭代会发散。
        T_des 为 None 时退化为几何雅可比（位置 + log(R) 微分，参考系 I）。
        """
        q = np.asarray(q, dtype=float)
        n = self.n_joints
        J = np.zeros((6, n))
        if T_des is None:
            T_des = np.eye(4)
        T_des = np.asarray(T_des, dtype=float)
        for k in range(n):
            dq = np.zeros(n)
            dq[k] = eps
            J[:, k] = (
                pose_error(self.fk(q + dq), T_des) - pose_error(self.fk(q - dq), T_des)
            ) / (2.0 * eps)
        return J


def rotation_error(R: np.ndarray, R_des: np.ndarray) -> np.ndarray:
    """R 相对目标 R_des 的姿态误差旋转矢量（基坐标下）。

    e = log(R_err)，R_err = R_des @ R^T，即“当前姿态还需绕基系轴怎么转”。
    角幅值 ∈ [0, π]，除对径点 π 外连续。
    """
    R_err = R_des @ R.T
    cos_ang = np.clip((np.trace(R_err) - 1.0) / 2.0, -1.0, 1.0)
    angle = np.arccos(cos_ang)
    if angle < 1e-10:
        return np.zeros(3)
    w = 0.5 * np.array(
        [R_err[2, 1] - R_err[1, 2], R_err[0, 2] - R_err[2, 0], R_err[1, 0] - R_err[0, 1]]
    ) / np.sin(angle)
    return angle * w


def pose_error(T: np.ndarray, T_des: np.ndarray) -> np.ndarray:
    """6 维位姿误差 [位置 3；旋转矢量 3]。"""
    e = np.zeros(6)
    e[:3] = T_des[:3, 3] - T[:3, 3]
    e[3:] = rotation_error(T[:3, :3], T_des[:3, :3])
    return e
