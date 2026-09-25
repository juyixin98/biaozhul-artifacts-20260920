"""自适应积分纯后端计算库。

只依赖 Python 标准库与 NumPy。核心算法（Gauss-Kronrod 7-15 与自适应 Simpson）
均自行实现，不调用任何外部求积库。

公开入口：
    integrate(...)          -> IntegrationResult
    integrate_request(...)  -> IntegrationResult（取 JSON 风格 dict）
    request_to_json / result_to_dict
"""

from .config import IntegrationConfig, IntegratorError
from .result import (
    IntegrationResult,
    STATUS_CONVERGED,
    STATUS_FAILED,
    ERROR_MESSAGES,
)
from .api import integrate, integrate_request
from .io_layer import request_to_json, result_to_dict, parse_request

__all__ = [
    "IntegrationConfig",
    "IntegratorError",
    "IntegrationResult",
    "STATUS_CONVERGED",
    "STATUS_FAILED",
    "ERROR_MESSAGES",
    "integrate",
    "integrate_request",
    "request_to_json",
    "result_to_dict",
    "parse_request",
]
