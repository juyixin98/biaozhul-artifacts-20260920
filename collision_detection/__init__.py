"""连续碰撞检测（Continuous Collision Detection）纯后端离线库。

只依赖 NumPy，输入为合成轨迹 / 传感器数据，不连接任何硬件。
"""

from .models import CircleBody
from .collision import (
    ContactInterval,
    analyze_pair,
    analyze_request,
    earliest_collision,
)
from .quadratic import solve_quadratic
from .trajectory import fit_linear_trajectory
from .io_json import load_request, build_response, CollisionRequest

__all__ = [
    "CircleBody",
    "ContactInterval",
    "analyze_pair",
    "analyze_request",
    "earliest_collision",
    "solve_quadratic",
    "fit_linear_trajectory",
    "load_request",
    "build_response",
    "CollisionRequest",
]
