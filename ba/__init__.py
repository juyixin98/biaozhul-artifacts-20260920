"""小规模单目束调整（Bundle Adjustment）核心库。

纯 NumPy/SciPy 实现，不依赖任何现成 SLAM 系统。
"""

from .problem import BAProblem, CameraPose, Intrinsics, Observation
from .solver import BASolver, SolverOptions, SolveResult

__all__ = [
    "BAProblem",
    "CameraPose",
    "Intrinsics",
    "Observation",
    "BASolver",
    "SolverOptions",
    "SolveResult",
]
