"""SE(2) pose graph optimization backend."""

from .se2 import wrap_angle, compose, inverse, between
from .optimizer import Edge, OptimizeOptions, OptimizeResult, optimize

__all__ = [
    "wrap_angle",
    "compose",
    "inverse",
    "between",
    "Edge",
    "OptimizeOptions",
    "OptimizeResult",
    "optimize",
]

__version__ = "0.1.0"
