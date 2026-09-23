"""二维固定宽条带装箱（矩形不旋转）计算库。

公开接口见 :func:`strip_packing.solver.solve_packing`。
"""

from .geometry import Tolerance, Placement, rects_overlap, verify_layout
from .validation import PackingError, validate_instance, Limits
from .lower_bounds import lower_bounds
from .solver import solve_packing, PackingSolution

__all__ = [
    "Tolerance",
    "Placement",
    "rects_overlap",
    "verify_layout",
    "PackingError",
    "validate_instance",
    "Limits",
    "lower_bounds",
    "solve_packing",
    "PackingSolution",
]
