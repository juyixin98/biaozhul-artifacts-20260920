"""二维位姿图优化（SE2 pose-graph optimization）纯后端库。

仅依赖 NumPy，使用合成数据，不连接任何硬件。
"""

from .se2 import wrap_angle, rotation_matrix, invert_pose, compose_pose, pose_error
from .kernels import Kernel, kernel_weight, robust_cost
from .graph import Node, Edge, PoseGraph, connected_components
from .optimizer import OptimizerOptions, OptimizeResult, optimize
from .io_json import load_request, dump_response, ParseError

__all__ = [
    "wrap_angle",
    "rotation_matrix",
    "invert_pose",
    "compose_pose",
    "pose_error",
    "Kernel",
    "kernel_weight",
    "robust_cost",
    "Node",
    "Edge",
    "PoseGraph",
    "connected_components",
    "OptimizerOptions",
    "OptimizeResult",
    "optimize",
    "load_request",
    "dump_response",
    "ParseError",
]
