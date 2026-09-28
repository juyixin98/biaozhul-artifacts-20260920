"""错误分类法。

四类必须可区分（HTTP 映射由 API 层负责）：
- input_error         400  输入数据本身不合法
- state_conflict      409  运行标识状态冲突（幂等键冲突）
- resource_exhausted  413  次数/迭代等资源上限
- computation_failed  422  数值计算失败（NaN / LAPACK 失败 / 产生不出可用结果）

注意：迭代耗尽但保留了部分根不属于异常，结果以 status="not_converged"
正常返回（见 models.SolveStatus）。
"""

from __future__ import annotations

import math
from typing import Any


class ErrorCategory:
    INPUT = "input_error"
    STATE = "state_conflict"
    RESOURCE = "resource_exhausted"
    COMPUTATION = "computation_failed"


class PolyrootError(Exception):
    """所有可预期错误的基类，携带机器可读 code 与结构化 details。"""

    category: str = ErrorCategory.INPUT
    code: str = "polyroot_error"
    http_status: int = 400

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category,
            "message": self.message,
            "details": _json_safe(self.details),
        }


def _json_safe(obj: Any) -> Any:
    """递归把 NaN/Inf 转成字符串标记，保证错误响应本身总能被 JSON 序列化。

    非有限值常出现在 details 回显里（如被拒绝的 NaN 系数），若直接序列化会让
    “报告非法输入”的响应自身崩溃。
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else f"non-finite:{obj!s}"
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


class InvalidCoefficientError(PolyrootError):
    code = "invalid_coefficients"


class NonFiniteCoefficientError(PolyrootError):
    code = "non_finite_coefficient"


class ZeroPolynomialError(PolyrootError):
    code = "zero_polynomial"


class InvalidOptionError(PolyrootError):
    code = "invalid_option"


class ConflictingStateError(PolyrootError):
    category = ErrorCategory.STATE
    code = "run_id_conflict"
    http_status = 409


class ResourceExhaustedError(PolyrootError):
    category = ErrorCategory.RESOURCE
    code = "resource_exhausted"
    http_status = 413


class ComputationFailedError(PolyrootError):
    category = ErrorCategory.COMPUTATION
    code = "computation_failed"
    http_status = 422
