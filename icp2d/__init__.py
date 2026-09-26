"""icp2d -- offline point-to-point 2D ICP scan matching library."""

from .degeneracy import DegeneracyReport
from .geometry import (
    compose_poses,
    inverse_pose,
    pose_error,
    rotation_matrix,
    transform_points,
)
from .icp import ICPConfig, ICPResult, estimate_pose
from .json_entry import run_request
from .synthetic import SyntheticScenario, make_scan_pair

__version__ = "0.1.0"

__all__ = [
    "DegeneracyReport",
    "ICPConfig",
    "ICPResult",
    "SyntheticScenario",
    "compose_poses",
    "estimate_pose",
    "inverse_pose",
    "make_scan_pair",
    "pose_error",
    "rotation_matrix",
    "run_request",
    "transform_points",
]
