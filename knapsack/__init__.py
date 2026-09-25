"""精确 0/1 背包求解库（整数重量/价值，分支定界 + 可验证上界）。"""

from .solver import SolveResult, solve, STATUS_OPTIMAL, STATUS_FEASIBLE
from .api import solve_request
from .brute import brute_force

__all__ = [
    "SolveResult",
    "solve",
    "solve_request",
    "brute_force",
    "STATUS_OPTIMAL",
    "STATUS_FEASIBLE",
]

__version__ = "0.1.0"
