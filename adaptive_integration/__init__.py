"""自适应积分纯后端计算库。

公开入口：
    integrate           —— 直接对 Python 可调用函数积分；
    ParsedFunction      —— 安全解析字符串表达式；
    handle_request      —— JSON 请求 -> JSON 响应；
    QuadResult          —— 结果数据类（含 converged/error_code 等）。
"""

from .api import handle_request
from .core import (DEFAULT_MAX_DEPTH, DEFAULT_MAX_EVALS, LIMIT_MAX_DEPTH,
                   LIMIT_MAX_EVALS, METHODS, IntegrationError, QuadResult,
                   integrate)
from .parser import EvalError, ParseError, ParsedFunction

__all__ = [
    "integrate", "QuadResult", "IntegrationError",
    "ParsedFunction", "ParseError", "EvalError", "handle_request",
    "METHODS", "DEFAULT_MAX_DEPTH", "DEFAULT_MAX_EVALS",
    "LIMIT_MAX_DEPTH", "LIMIT_MAX_EVALS",
]
