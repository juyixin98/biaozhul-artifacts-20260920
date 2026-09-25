"""JSON 接口：请求字典 -> 响应字典。

响应状态：
- "optimal": 间隙闭合，value 已证明最优，upper_bound == value。
- "feasible": 超时或达到节点上限，返回当前可行解与仍有效的全局上界；
  此时 gap = upper_bound - value > 0，绝不声称最优。
- "error": 输入不合法，error.message 说明原因。
"""

from __future__ import annotations

from typing import Any, Dict

import numpy as np

from .solver import STATUS_FEASIBLE, STATUS_OPTIMAL, solve
from .validate import ValidationError, validate_request


def solve_request(req: Any) -> Dict[str, Any]:
    """处理一个求解请求（已解析的 JSON 对象），返回可 JSON 序列化的响应。"""
    try:
        ids, weights, values, capacity, time_limit_sec, max_nodes = validate_request(req)
    except ValidationError as exc:
        return {
            "status": "error",
            "error": {"code": "invalid_input", "message": str(exc)},
        }

    w = np.asarray(weights, dtype=np.int64)
    v = np.asarray(values, dtype=np.int64)

    result = solve(
        w, v, capacity,
        time_limit_sec=time_limit_sec,
        max_nodes=max_nodes,
    )

    selected = result.selected
    total_weight = int(w[selected].sum()) if selected else 0
    if result.status == STATUS_OPTIMAL:
        message = "proved optimal: branch-and-bound tree fully explored, gap closed to 0"
    elif result.status == STATUS_FEASIBLE:
        message = (
            f"gap not closed ({result.reason}); returning best feasible solution "
            f"with a still-valid upper bound"
        )
    else:  # 防御：不应到达
        message = result.reason

    return {
        "status": result.status,
        "value": result.value,
        "upper_bound": result.upper_bound,
        "gap": result.gap,
        "selected_ids": [ids[i] for i in selected],
        "selected_indices": selected,
        "total_weight": total_weight,
        "capacity": capacity,
        "nodes": result.nodes,
        "elapsed_ms": round(result.elapsed_ms, 3),
        "message": message,
    }
