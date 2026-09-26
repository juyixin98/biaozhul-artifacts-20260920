"""运动轨迹速度约束离线计算库。

只依赖 NumPy，输入为合成折线与合成传感器数据，不连接任何硬件。
"""

from .path import Polyline, build_polyline, segment_lengths, turning_angles
from .limits import CornerConfig, Dynamics, node_speed_bounds
from .kinematics import (
    InfeasibleTrajectory,
    SegmentPlan,
    plan_segment,
    propagate_node_speeds,
)
from .profile import (
    TrajectoryProfile,
    sample_at,
    sample_grid,
    validate_profile,
)
from .synthetic import simulate_odometry
from .planner import build_profile, plan_from_request

__all__ = [
    "Polyline",
    "build_polyline",
    "segment_lengths",
    "turning_angles",
    "Dynamics",
    "CornerConfig",
    "node_speed_bounds",
    "InfeasibleTrajectory",
    "SegmentPlan",
    "plan_segment",
    "propagate_node_speeds",
    "TrajectoryProfile",
    "sample_at",
    "sample_grid",
    "validate_profile",
    "simulate_odometry",
    "build_profile",
    "plan_from_request",
]
