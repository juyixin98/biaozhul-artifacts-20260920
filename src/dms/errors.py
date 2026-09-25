"""异常层次。

安全约定：所有异常消息只允许携带**规则元数据**（规则 id、字段路径、参数名等
schema 级信息），严禁携带文档中的字段值。调用方在构造异常时必须遵守。
"""

from __future__ import annotations

from typing import Any


class DMSError(Exception):
    """本项目所有异常的基类。"""

    code = "dms_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class RuleCompileError(DMSError):
    """规则集编译失败（结构、参数、未知动作/字段等）。"""

    code = "rule_compile_error"


class UnknownRuleError(RuleCompileError):
    """出现未知规则动作（默认拒绝）。"""

    code = "unknown_rule"


class RuleConflictError(DMSError):
    """同优先级规则作用域重叠且无法确定先后（fail-closed）。"""

    code = "rule_conflict"


class MissingFieldError(DMSError):
    """require_match 的规则在文档中没有任何匹配。"""

    code = "missing_field"


class TransformError(DMSError):
    """变换执行失败（类型不符、解密失败等）。消息中不含原始值。"""

    code = "transform_error"


class InvalidPayloadError(DMSError):
    """HTTP 层请求体不是合法 JSON 或超出大小限制。"""

    code = "invalid_payload"
