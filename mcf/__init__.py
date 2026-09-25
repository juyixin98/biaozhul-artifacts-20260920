"""最小费用流计算库（纯后端）。

对外核心接口：

- :class:`mcf.graph.FlowNetwork` —— 有向流网络（容量/费用均为非负/有符号整数）。
- :func:`mcf.solver.min_cost_max_flow` —— 最小费用最大流（Successive Shortest Path + 势函数 Dijkstra）。
- :class:`mcf.api.MCFError` / :func:`mcf.api.solve_request` —— JSON 请求校验与求解。
"""

from .graph import FlowNetwork
from .solver import FlowResult, min_cost_max_flow
from .api import MCFError, solve_request

__all__ = [
    "FlowNetwork",
    "FlowResult",
    "min_cost_max_flow",
    "MCFError",
    "solve_request",
]
