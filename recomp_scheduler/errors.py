"""错误契约。

所有由本包主动抛出的异常都继承 :class:`RecompError`，并携带一个稳定的
``category`` 字符串（用于 API 响应与日志分类）。调用方（含测试）应当依赖
错误类别，而不是异常消息文本。

类别一览
--------
- ``input_error``       输入形状/取值/图结构非法（:class:`GraphValidationError`）
- ``invalid_plan``      检查点方案本身非法，或方案与图不匹配（:class:`InvalidPlanError`）
- ``state_conflict``    训练状态冲突：RNG 未记录/已记录、运行重复推进、快照缺失
                         （:class:`StateConflictError`）
- ``resource_exhausted`` 内存预算不可行或运行时超出预算（:class:`BudgetInfeasibleError`）
- ``compute_failure``   数值/算子执行失败（:class:`ComputeFailureError`）
"""

from __future__ import annotations

from typing import Any, Mapping


class RecompError(Exception):
    """所有本包错误的基类。

    Attributes
    ----------
    category:
        稳定的机器可读错误类别。
    details:
        结构化上下文（必须可 JSON 序列化），供日志与 API 使用；
        不得包含真实业务数据——本项目全部为本地合成数据。
    """

    category: str = "error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 ``{"category", "message", "details"}``。"""
        return {
            "category": self.category,
            "message": str(self.message),
            "details": _json_safe(self.details),
        }


class GraphValidationError(RecompError):
    """输入错误：节点/边/形状/算子配置非法。"""

    category = "input_error"


class InvalidPlanError(RecompError):
    """方案错误：检查点方案结构非法或不适用于给定图。"""

    category = "invalid_plan"


class StateConflictError(RecompError):
    """状态冲突：RNG 记录重复/缺失、运行非法推进、快照引用不一致。"""

    category = "state_conflict"


class BudgetInfeasibleError(RecompError):
    """资源耗尽：静态判定预算不可行，或运行时峰值超过预算。"""

    category = "resource_exhausted"


class ComputeFailureError(RecompError):
    """计算失败：算子执行或数值校验失败（NaN/Inf/梯度不一致）。"""

    category = "compute_failure"


def _json_safe(obj: Any) -> Any:
    """把错误上下文中的 numpy 类型转换为可 JSON 序列化的原生类型。"""
    if isinstance(obj, Mapping):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if hasattr(obj, "item"):  # numpy 标量
        try:
            return obj.item()
        except (ValueError, TypeError):
            return repr(obj)
    return repr(obj)
