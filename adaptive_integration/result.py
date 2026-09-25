"""结果对象与失败状态定义。

任何路径都返回结构化结果：converged=False 时附带机器可读错误码与
人类可读说明，绝不静默返回一个看似正常的数值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


STATUS_CONVERGED = "converged"
STATUS_FAILED = "failed"

# 错误码 -> 默认中文说明（调用方可以附带定位细节）
ERROR_MESSAGES = {
    "INVALID_REQUEST": "请求参数不合法",
    "PARSE_ERROR": "被积函数表达式无法解析",
    "DEPTH_LIMIT_REACHED": (
        "达到最大细分深度仍未满足误差预算：被积函数可能含端点奇点"
        "或在极小尺度上剧烈变化"
    ),
    "EVALUATION_BUDGET_EXHAUSTED": (
        "达到函数求值次数硬上限：积分在给定预算下不收敛"
    ),
    "INVALID_VALUE_AT_POINT": (
        "在积分节点处被积函数返回非有限值（inf/NaN）："
        "积分区间内或端点存在奇点"
    ),
    "ROUND_OFF_NO_PROGRESS": (
        "继续细分误差估计不再下降：已达到浮点舍入误差底噪，"
        "或遇到不可积奇点，无法达到请求容差"
    ),
}


@dataclass
class IntegrationResult:
    status: str                          # 'converged' | 'failed'
    value: Optional[float] = None       # 失败时为当前最优近似（可能不可靠）
    error_estimate: Optional[float] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    method: str = ""
    evaluations: int = 0
    depth_reached: int = 0
    intervals: int = 0
    warnings: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def converged(self) -> bool:
        return self.status == STATUS_CONVERGED

    def to_dict(self) -> dict[str, Any]:
        """转换为可严格 json.dumps 的字典（含嵌套结构中的非有限值）。"""

        def _sanitize(v):
            if isinstance(v, float):
                if v != v:
                    return "NaN"
                if v == float("inf"):
                    return "Infinity"
                if v == float("-inf"):
                    return "-Infinity"
                return v
            if isinstance(v, dict):
                return {k: _sanitize(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [_sanitize(x) for x in v]
            return v

        out: dict[str, Any] = {
            "status": self.status,
            "method": self.method,
            "result": _sanitize(float(self.value))
                      if isinstance(self.value, (int, float)) else None,
            "error_estimate": _sanitize(float(self.error_estimate))
                      if isinstance(self.error_estimate, (int, float))
                      else None,
            "evaluations": self.evaluations,
            "depth_reached": self.depth_reached,
            "intervals": self.intervals,
            "warnings": list(self.warnings),
            "diagnostics": _sanitize(dict(self.diagnostics)),
        }
        if not self.converged:
            out["error_code"] = self.error_code
            out["error_message"] = self.error_message
        return out
