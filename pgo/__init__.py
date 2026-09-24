"""SE(2) pose graph optimization service (NumPy/SciPy, pure backend)."""

from .se2 import compose, inverse, between, wrap_angle, rotation2
from .kernels import KERNELS, kernel_scales
from .optimizer import (
    Edge,
    PoseGraph,
    OptimizeOptions,
    OptimizeResult,
    optimize,
    edge_residual,
    edge_jacobians,
    edge_residual_and_jacobians,
    numerical_jacobian,
)
from .synthetic import build_synthetic_graph

__all__ = [
    "compose",
    "inverse",
    "between",
    "wrap_angle",
    "rotation2",
    "KERNELS",
    "kernel_scales",
    "Edge",
    "PoseGraph",
    "OptimizeOptions",
    "OptimizeResult",
    "optimize",
    "edge_residual",
    "edge_jacobians",
    "edge_residual_and_jacobians",
    "build_synthetic_graph",
]
