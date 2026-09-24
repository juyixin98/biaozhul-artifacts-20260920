"""Offline TF tree query service: timestamped SE(3) transform cache.

Pure synthetic-data / offline-replay library. No hardware, no visualization.
"""

from .exceptions import (
    TFError,
    CycleError,
    LookupError_,
    ExtrapolationError,
    ConnectivityError,
)
from .transform import SE3
from .buffer import TransformBuffer
from .tree import TransformTree

__all__ = [
    "TFError",
    "CycleError",
    "LookupError_",
    "ExtrapolationError",
    "ConnectivityError",
    "SE3",
    "TransformBuffer",
    "TransformTree",
]

__version__ = "0.1.0"
