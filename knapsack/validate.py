"""输入校验：明确输入范围与失败状态。

输入范围（小中规模限定）：
- 物品数 n: 0 <= n <= MAX_ITEMS (500)
- 容量 capacity: 整数，0 <= capacity <= MAX_ABS (10^12)
- 重量 weight: 整数，0 <= weight <= MAX_ABS（允许 0，即零重量物品）
- 价值 value: 整数，|value| <= MAX_ABS（允许负价值）
- 物品 id: 可选字符串，须唯一；缺省为 "item-<下标>"
- options.time_limit_ms: 整数，1 .. MAX_TIME_LIMIT_MS (60000)，默认 5000
- options.max_nodes: 整数，1 .. MAX_NODES (5_000_000)，默认 200_000

所有数值必须为 JSON 整数（Python int）；布尔值与浮点数一律拒绝，
避免隐式取整造成的静默错误。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

MAX_ITEMS = 500
MAX_ABS = 10**12
MAX_TIME_LIMIT_MS = 60_000
MAX_NODES = 5_000_000
DEFAULT_TIME_LIMIT_MS = 5_000
DEFAULT_MAX_NODES = 200_000


class ValidationError(ValueError):
    """输入不合法；message 面向调用者，说明具体字段与约束。"""


def _check_int(name: str, x: Any, lo: int, hi: int) -> int:
    if isinstance(x, bool) or not isinstance(x, int):
        raise ValidationError(f"{name} must be an integer, got {type(x).__name__}: {x!r}")
    if not (lo <= x <= hi):
        raise ValidationError(f"{name} must be in [{lo}, {hi}], got {x}")
    return x


def validate_request(req: Any) -> Tuple[List[str], List[int], List[int], int, float, int]:
    """校验请求字典，返回 (ids, weights, values, capacity, time_limit_sec, max_nodes)。

    不合法时抛出 ValidationError。
    """
    if not isinstance(req, dict):
        raise ValidationError(f"request must be a JSON object, got {type(req).__name__}")

    if "capacity" not in req:
        raise ValidationError("missing required field: capacity")
    capacity = _check_int("capacity", req["capacity"], 0, MAX_ABS)

    items = req.get("items")
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ValidationError(f"items must be an array, got {type(items).__name__}")
    if len(items) > MAX_ITEMS:
        raise ValidationError(f"items length must be <= {MAX_ITEMS}, got {len(items)}")

    ids: List[str] = []
    weights: List[int] = []
    values: List[int] = []
    seen_ids = set()
    for k, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValidationError(f"items[{k}] must be an object, got {type(item).__name__}")
        if "weight" not in item or "value" not in item:
            raise ValidationError(f"items[{k}] requires both 'weight' and 'value'")
        w = _check_int(f"items[{k}].weight", item["weight"], 0, MAX_ABS)
        v = _check_int(f"items[{k}].value", item["value"], -MAX_ABS, MAX_ABS)
        item_id = item.get("id", f"item-{k}")
        if not isinstance(item_id, str):
            raise ValidationError(f"items[{k}].id must be a string, got {type(item_id).__name__}")
        if item_id in seen_ids:
            raise ValidationError(f"duplicate item id: {item_id!r}")
        seen_ids.add(item_id)
        ids.append(item_id)
        weights.append(w)
        values.append(v)

    options = req.get("options", {})
    if not isinstance(options, dict):
        raise ValidationError(f"options must be an object, got {type(options).__name__}")
    time_limit_ms = _check_int(
        "options.time_limit_ms", options.get("time_limit_ms", DEFAULT_TIME_LIMIT_MS),
        1, MAX_TIME_LIMIT_MS,
    )
    max_nodes = _check_int(
        "options.max_nodes", options.get("max_nodes", DEFAULT_MAX_NODES),
        1, MAX_NODES,
    )

    return ids, weights, values, capacity, time_limit_ms / 1000.0, max_nodes
