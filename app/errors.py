"""统一错误契约。

四类可区分错误（模块间数据与错误契约的一部分）：

- INPUT_ERROR        输入错误：请求体/规则语法/待词法文本不合法
- STATE_CONFLICT     状态冲突：与已持久化状态冲突（如重名规范、重复构建）
- RESOURCE_EXHAUSTED 资源耗尽：超出配置的资源上限
- COMPUTATION_FAILED 计算失败：内核内部计算失败（非输入原因）

HTTP 映射见 app/api/routes.py。
"""

from __future__ import annotations

import enum
from typing import Any


class ErrorCategory(str, enum.Enum):
    INPUT_ERROR = "input_error"
    STATE_CONFLICT = "state_conflict"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    COMPUTATION_FAILED = "computation_failed"


class AppError(Exception):
    """所有可预期错误的基类。携带类别、机器可读码与细节。"""

    category: ErrorCategory = ErrorCategory.COMPUTATION_FAILED

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = details or {}

    def to_payload(self) -> dict[str, Any]:
        return {
            "error": {
                "category": self.category.value,
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


class InputError(AppError):
    category = ErrorCategory.INPUT_ERROR


class StateConflict(AppError):
    category = ErrorCategory.STATE_CONFLICT


class ResourceExhausted(AppError):
    category = ErrorCategory.RESOURCE_EXHAUSTED


class ComputationFailed(AppError):
    category = ErrorCategory.COMPUTATION_FAILED
