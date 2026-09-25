"""精确 0/1 背包求解后端（纯后端，无前端）。

公开接口：
    solve(payload: dict) -> dict     # JSON 风格的字典入参 / 出参
    solve_knapsack(...)              # 底层计算接口
    ValidationError                  # 输入校验异常
"""

from .api import solve
from .solver import SolveResult, solve_knapsack
from .validation import Limits, ValidationError

__all__ = [
    "solve",
    "solve_knapsack",
    "SolveResult",
    "ValidationError",
    "Limits",
]

__version__ = "1.0.0"
