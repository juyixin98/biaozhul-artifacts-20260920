"""束调整问题数据结构。

约定：
- 相机位姿为世界到相机变换：p_c = R @ p_w + t。
- 内参固定（针孔模型，无畸变）。
- 观测为像素坐标 (u, v)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .lie import exp_so3, log_so3


@dataclass
class Intrinsics:
    fx: float
    fy: float
    cx: float
    cy: float


@dataclass
class CameraPose:
    """世界到相机的外参。"""

    R: np.ndarray  # (3, 3)
    t: np.ndarray  # (3,)

    @classmethod
    def from_rvec_tvec(cls, rvec, tvec) -> "CameraPose":
        return cls(R=exp_so3(np.asarray(rvec, dtype=float)), t=np.asarray(tvec, dtype=float).reshape(3))

    def to_rvec_tvec(self):
        return log_so3(self.R), self.t.copy()

    def transform(self, point: np.ndarray) -> np.ndarray:
        return self.R @ point + self.t


@dataclass
class Observation:
    camera: int
    point: int
    uv: np.ndarray  # (2,)


@dataclass
class BAProblem:
    intrinsics: Intrinsics
    cameras: list[CameraPose]
    points: np.ndarray  # (N, 3)
    observations: list[Observation] = field(default_factory=list)

    def copy(self) -> "BAProblem":
        return BAProblem(
            intrinsics=Intrinsics(**vars(self.intrinsics)),
            cameras=[CameraPose(c.R.copy(), c.t.copy()) for c in self.cameras],
            points=self.points.copy(),
            observations=[Observation(o.camera, o.point, o.uv.copy()) for o in self.observations],
        )
