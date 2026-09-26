"""二维圆形运动体数据模型。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class CircleBody:
    """沿线性轨迹运动的二维圆盘：center(t) = center + velocity * t。"""

    center: np.ndarray  # shape=(2,)，t=0 时圆心
    velocity: np.ndarray  # shape=(2,)，常速度矢量
    radius: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "center", np.asarray(self.center, dtype=float).reshape(2))
        object.__setattr__(self, "velocity", np.asarray(self.velocity, dtype=float).reshape(2))
        if not np.isfinite(self.center).all() or not np.isfinite(self.velocity).all():
            raise ValueError("圆心与速度必须为有限实数")
        if not np.isfinite(self.radius) or self.radius < 0.0:
            raise ValueError("半径必须为非负有限实数")

    def position_at(self, t: float) -> np.ndarray:
        return self.center + self.velocity * float(t)
