"""mospp — 多目标（双目标：时间/费用）最短路纯后端计算库。

公开接口：
    solve_request(request: dict) -> dict
    Graph / Solver                 底层类
    MOSPError                      校验异常
    status                         失败状态常量
"""

from .errors import MOSPError, status
from .graph import Graph
from .solver import Solver, Label, SolveLimits
from .api import solve_request, encode_response

__all__ = [
    "solve_request",
    "encode_response",
    "MOSPError",
    "status",
    "Graph",
    "Solver",
    "Label",
    "SolveLimits",
]
