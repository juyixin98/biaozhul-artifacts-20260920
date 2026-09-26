"""2D laser scan matching: point-to-point ICP offline computation library."""

from scan_matching.geometry import SE2Pose, apply_transform, compose, inverse
from scan_matching.icp import ICPParams, ICPResult, icp
from scan_matching.synthetic import (
    add_outliers,
    generate_scene,
    generate_scan,
    partial_overlap_scan,
)

__all__ = [
    "SE2Pose",
    "apply_transform",
    "compose",
    "inverse",
    "ICPParams",
    "ICPResult",
    "icp",
    "generate_scene",
    "generate_scan",
    "add_outliers",
    "partial_overlap_scan",
]

__version__ = "0.1.0"
