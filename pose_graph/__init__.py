"""2D pose graph optimization (SE2) backend.

Pure Python + NumPy library for offline nonlinear least-squares pose graph
optimization.  No hardware, no visualization, no ROS dependency.
"""

from .graph import Edge, Node, PoseGraph, GraphStructureError
from .optimizer import OptimizeOptions, OptimizeResult, optimize
from .json_io import run_from_dict, load_request, dump_response

__all__ = [
    "Edge",
    "Node",
    "PoseGraph",
    "GraphStructureError",
    "OptimizeOptions",
    "OptimizeResult",
    "optimize",
    "run_from_dict",
    "load_request",
    "dump_response",
]
