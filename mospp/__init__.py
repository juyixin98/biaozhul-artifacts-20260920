"""mospp —— 多目标（双目标：时间/费用）最短路计算库。

纯 Python + NumPy 实现，无第三方求解器依赖。
核心入口：

* :func:`mospp.api.run_request` —— 传入 JSON 风格字典，返回响应字典；
* :func:`mospp.labeling.solve` —— 库层直接求解。
"""

from __future__ import annotations

from . import api, enumeration, errors, graph, labeling, tolerance
from .api import run_request
from .graph import Graph
from .labeling import Label, SolveResult, solve

__all__ = [
    "api",
    "enumeration",
    "errors",
    "graph",
    "labeling",
    "tolerance",
    "run_request",
    "Graph",
    "Label",
    "SolveResult",
    "solve",
]

__version__ = "1.0.0"
