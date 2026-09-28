"""激活重计算调度 (activation recomputation scheduler)。

一个自包含的后端服务：对线性与带分支的前向计算图做激活检查点规划，
并在反向传播中通过重放随机状态完成重计算。

顶层包只导出稳定的公共接口与错误类型；模块间的数据/错误契约见各模块文档串。
"""

from .errors import (
    RecompError,
    GraphValidationError,
    InvalidPlanError,
    StateConflictError,
    BudgetInfeasibleError,
    ComputeFailureError,
)

__all__ = [
    "RecompError",
    "GraphValidationError",
    "InvalidPlanError",
    "StateConflictError",
    "BudgetInfeasibleError",
    "ComputeFailureError",
]

__version__ = "1.0.0"
