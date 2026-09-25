"""JSON 请求/响应接口。

请求格式（字段名 ``from``/``to``/``capacity``/``cost``）::

    {
      "n": 4,
      "source": 0,
      "sink": 3,
      "required_flow": 5,            // 可选；省略=求最大流
      "edges": [
        {"from": 0, "to": 1, "capacity": 10, "cost": 2}
      ]
    }

成功响应::

    {"status": "ok", "result": { ... }}

失败响应::

    {"status": "error", "error": {"code": "...", "message": "..."}}

错误码
------
- ``invalid_json``        原始 JSON 无法解析（CLI 层使用）
- ``invalid_request``     请求结构/字段类型/数值范围不合法
- ``negative_cycle``      存在从源点可达的负费用环（输入前提被破坏）

整数校验严格：JSON 的 ``true``/``1.5``/``"3"`` 均不接受为整数。
"""

from __future__ import annotations

import json
from typing import Any

from . import limits
from .graph import FlowNetwork
from .solver import NegativeCycleError, FlowResult, min_cost_max_flow


class MCFError(ValueError):
    """可映射为 JSON 错误响应的业务异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _is_int(value: Any) -> bool:
    """仅接受真正的整数（Python 的 bool 是 int 子类，显式排除）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(name: str, value: Any) -> int:
    if not _is_int(value):
        raise MCFError("invalid_request", f"字段 {name!r} 必须是整数")
    return int(value)


def _require_object(name: str, value: Any) -> dict:
    if not isinstance(value, dict):
        raise MCFError("invalid_request", f"字段 {name!r} 必须是对象")
    return value


def _require_list(name: str, value: Any) -> list:
    if not isinstance(value, list):
        raise MCFError("invalid_request", f"字段 {name!r} 必须是数组")
    return value


def parse_request(payload: Any) -> tuple[FlowNetwork, int | None]:
    """把已解析的 JSON 对象转换为 :class:`FlowNetwork` 和目标流量。"""
    data = _require_object("request", payload)

    required = {"n", "source", "sink", "edges"}
    missing = required - data.keys()
    if missing:
        raise MCFError(
            "invalid_request", f"缺少必需字段: {', '.join(sorted(missing))}"
        )

    n = _require_int("n", data["n"])
    source = _require_int("source", data["source"])
    sink = _require_int("sink", data["sink"])
    raw_edges = _require_list("edges", data["edges"])

    # 先做不依赖网络对象的结构校验（n>=2 时才能安全建邻接表）。
    if not isinstance(n, int) or n < 2:
        raise MCFError("invalid_request", "顶点数 n 必须是 >= 2 的整数")
    if n > limits.MAX_NODES:
        raise MCFError("invalid_request", f"顶点数超过上限 {limits.MAX_NODES}")
    if not (0 <= source < n and 0 <= sink < n):
        raise MCFError(
            "invalid_request",
            f"源点/汇点编号必须在 [0, {n - 1}] 内 "
            f"(source={source}, sink={sink})",
        )
    if source == sink:
        raise MCFError("invalid_request", "源点与汇点不能相同")
    if len(raw_edges) > limits.MAX_EDGES:
        raise MCFError(
            "invalid_request", f"边数超过上限 {limits.MAX_EDGES}"
        )

    required_flow: int | None = None
    if "required_flow" in data and data["required_flow"] is not None:
        required_flow = _require_int("required_flow", data["required_flow"])
        if not (0 <= required_flow <= limits.MAX_REQUIRED_FLOW):
            raise MCFError(
                "invalid_request",
                f"required_flow 超出允许范围 [0, {limits.MAX_REQUIRED_FLOW}]",
            )

    edges: list[tuple[int, int, int, int]] = []
    for idx, raw in enumerate(raw_edges):
        obj = _require_object(f"edges[{idx}]", raw)
        for field_name in ("from", "to", "capacity", "cost"):
            if field_name not in obj:
                raise MCFError(
                    "invalid_request",
                    f"edges[{idx}] 缺少字段 {field_name!r}",
                )
        u = _require_int(f"edges[{idx}].from", obj["from"])
        v = _require_int(f"edges[{idx}].to", obj["to"])
        cap = _require_int(f"edges[{idx}].capacity", obj["capacity"])
        cost = _require_int(f"edges[{idx}].cost", obj["cost"])
        if not (0 <= u < n and 0 <= v < n):
            raise MCFError(
                "invalid_request",
                f"edges[{idx}] 顶点编号 ({u}->{v}) 超出 [0, {n - 1}]",
            )
        edges.append((u, v, cap, cost))

    net = FlowNetwork(n, source, sink, edges)
    try:
        net.validate_static()
    except ValueError as exc:
        raise MCFError("invalid_request", str(exc)) from exc
    return net, required_flow


def solve_request(payload: Any) -> dict:
    """求解一个已解析的 JSON 请求，返回可序列化的响应字典。"""
    net, required_flow = parse_request(payload)
    try:
        result: FlowResult = min_cost_max_flow(net, required_flow)
    except NegativeCycleError as exc:
        raise MCFError("negative_cycle", str(exc)) from exc

    # 交付前自检：容量约束 + 流守恒（失败说明算法实现有误，直接暴露）。
    result.verify()
    return {"status": "ok", "result": result.to_dict()}


def error_response(code: str, message: str) -> dict:
    return {"status": "error", "error": {"code": code, "message": message}}


def process_json_text(text: str) -> tuple[dict, int]:
    """解析 JSON 字符串并求解；返回 ``(响应字典, HTTP风格退出码)``。

    退出码：0 成功；2 输入/业务错误（含负环）；1 不应发生的内部错误。
    """
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return error_response("invalid_json", f"JSON 解析失败: {exc}"), 2

    try:
        return solve_request(payload), 0
    except MCFError as exc:
        return error_response(exc.code, exc.message), 2
