"""输入范围与数值容差。

本求解器只接受**整数**重量与整数价值：分支定界中的关键比较全部使用
Python 任意精度整数（以及分子/分母形式的精确有理数上界），因此求解
路径上不存在浮点舍入误差。浮点数仅用于：

* ``timeout_seconds``（挂钟时间限制，本身就是近似量）；
* 输出中的 ``relative_gap`` 展示字段，计算时使用固定的展示容差
  ``GAP_EPS = 1e-12``，仅用于把本应为 0 的间隙显示为 0.0。

输入范围在 :class:`Limits` 中集中定义，求解器对超出范围的输入直接返回
结构化校验错误，而不是隐式截断或静默接受。
"""

from __future__ import annotations

from dataclasses import dataclass


# 展示用容差：relative_gap < GAP_EPS 时显示为 0.0。不参与任何求解判定。
GAP_EPS = 1e-12

# timeout_seconds 的允许区间（秒）。
MIN_TIMEOUT = 1e-6
MAX_TIMEOUT = 300.0
DEFAULT_TIMEOUT = 5.0


@dataclass(frozen=True)
class Limits:
    """硬性输入范围（构造非法实例会被 :class:`ValidationError` 拒绝）。"""

    max_items: int = 2000          # 本求解器限定的小中规模上限
    min_capacity: int = 0
    max_capacity: int = 10**12
    min_weight: int = 0
    max_weight: int = 10**9
    min_value: int = -(10**9)
    max_value: int = 10**9

    # 求解过程中的中间量（利润和）理论上至多 max_items * max_value，
    # 2 * 10^12 远在 Python 大整数的舒适区内，不存在溢出问题。


class ValidationError(ValueError):
    """输入不满足 :class:`Limits` 约束。``field`` 为出错字段名。"""

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.message = message
        self.field = field


def _is_plain_int(x: object) -> bool:
    """仅接受真正的 int（bool 是 int 的子类，需排除）。"""

    return isinstance(x, int) and not isinstance(x, bool)


def require_int(value: object, field: str,
                lo: int | None = None, hi: int | None = None) -> int:
    if not _is_plain_int(value):
        raise ValidationError(f"{field!r} 必须是整数，实际类型为 {type(value).__name__}", field)
    if lo is not None and value < lo:
        raise ValidationError(f"{field!r} 必须 >= {lo}，实际为 {value}", field)
    if hi is not None and value > hi:
        raise ValidationError(f"{field!r} 必须 <= {hi}，实际为 {value}", field)
    return value


def require_int_list(value: object, field: str, limits: Limits) -> list[int]:
    if not isinstance(value, list):
        raise ValidationError(f"{field!r} 必须是 JSON 数组(list[int])，实际类型为 {type(value).__name__}", field)
    out: list[int] = []
    for i, x in enumerate(value):
        item_field = f"{field}[{i}]"
        if not _is_plain_int(x):
            raise ValidationError(f"{item_field} 必须是整数，实际类型为 {type(x).__name__}", item_field)
        out.append(x)
    return out


def validate_payload(data: object, limits: Limits | None = None) -> dict:
    """校验 JSON 风格请求字典，返回规范化后的关键字参数字典。

    接受字段::

        capacity     : int   [0, 1e12]
        weights      : list[int]，长度 [0, 2000]，元素 [0, 1e9]
        values       : list[int]，长度 [0, 2000]，元素 [-1e9, 1e9]
        timeout_seconds : float，(0, 300]，可选（默认 5.0）

    顶层必须是 JSON 对象（dict）。weights 与 values 长度必须相等。
    """

    lim = limits or Limits()

    if not isinstance(data, dict):
        raise ValidationError("请求体必须是 JSON 对象，例如 "
                              '{"capacity": 10, "weights": [5], "values": [3]}',
                              field="<body>")

    if "capacity" not in data:
        raise ValidationError("缺少必填字段 'capacity'", field="capacity")
    if "weights" not in data:
        raise ValidationError("缺少必填字段 'weights'", field="weights")
    if "values" not in data:
        raise ValidationError("缺少必填字段 'values'", field="values")

    capacity = require_int(data["capacity"], "capacity",
                           lim.min_capacity, lim.max_capacity)
    weights = require_int_list(data["weights"], "weights", lim)
    values = require_int_list(data["values"], "values", lim)

    n = len(weights)
    if n > lim.max_items:
        raise ValidationError(
            f"'weights' 长度 {n} 超过小中规模上限 {lim.max_items}",
            field="weights",
        )
    if len(values) != n:
        raise ValidationError(
            f"'weights' 与 'values' 长度必须相等：{n} != {len(values)}",
            field="values",
        )
    for i, w in enumerate(weights):
        require_int(w, f"weights[{i}]", lim.min_weight, lim.max_weight)
    for i, v in enumerate(values):
        require_int(v, f"values[{i}]", lim.min_value, lim.max_value)

    timeout = DEFAULT_TIMEOUT
    if "timeout_seconds" in data and data["timeout_seconds"] is not None:
        t = data["timeout_seconds"]
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            raise ValidationError(
                f"'timeout_seconds' 必须是正数(秒)，实际类型为 {type(t).__name__}",
                field="timeout_seconds",
            )
        timeout = float(t)
        if not (MIN_TIMEOUT <= timeout <= MAX_TIMEOUT):
            raise ValidationError(
                f"'timeout_seconds' 必须在 [{MIN_TIMEOUT}, {MAX_TIMEOUT}] 内，实际为 {timeout}",
                field="timeout_seconds",
            )

    return {
        "capacity": capacity,
        "weights": weights,
        "values": values,
        "timeout_seconds": timeout,
        "limits": lim,
    }
