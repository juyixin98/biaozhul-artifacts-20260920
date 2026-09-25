"""trajvel: offline time parameterization of polyline trajectories.

Pure-computation library (NumPy only): given a polyline (waypoints) and
velocity/acceleration limits, compute a time parameterization that respects
the limits, with corner speed limiting via an explicit curvature model or a
stop-at-corner strategy.
"""

from .geometry import dedupe_points, segment_lengths, turning_angles
from .parameterize import (
    CornerStrategy,
    ParameterizationResult,
    parameterize,
    segment_duration,
)
from .sampling import sample_trajectory

__all__ = [
    "CornerStrategy",
    "ParameterizationResult",
    "dedupe_points",
    "parameterize",
    "sample_trajectory",
    "segment_duration",
    "segment_lengths",
    "turning_angles",
]

__version__ = "0.1.0"
