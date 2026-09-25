"""JSON 请求/响应接口（纯函数，无 HTTP 框架）。

请求格式
========
``solve_request(dict)`` 接受如下 JSON 对象（键名见 README）：

============== ============================================================
字段           说明
============== ============================================================
nodes          可选；顶点 ID 数组（字符串/整数，不可为布尔）
edges          必填；``{"from","to","time","cost","key"?}`` 数组
directed       可选，默认 true；false 时每条边展开为双向两条弧
source/target  必填；图中顶点
time_budget    可选；非负有限数，超出该时间的标签被剪枝
cost_budget    可选；同上（费用）
label_cap      可选；1..max_label_cap 的正整数，默认 200000
eps            可选；容差，范围 [0, 1e-2]，默认 1e-9
============== ============================================================

响应格式
========
成功（ok / truncated / unreachable）::

    {
      "status": "...",
      "paths": [{"nodes":[...], "edges":[...], "time":t, "cost":c}, ...],
      "pareto": [{"time":t,"cost":c}, ...],
      "stats": {...}, "parameters": {...}
    }

失败（invalid_request / limit_exceeded / internal_error）::

    {"status": "...", "error": "中文说明", "paths": [], ...}
"""

from __future__ import annotations

import math

from .errors import MOSPError, status
from .graph import Graph
from .solver import Solver, SolveLimits
from .tolerance import DEFAULT_EPS, MAX_EPS, MIN_EPS


def _require_optional_number(req, name, limits: SolveLimits):
    if name not in req or req[name] is None:
        return None
    x = req[name]
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise MOSPError(f"{name} 必须是数字，得到 {x!r}", status.INVALID_REQUEST)
    xf = float(x)
    if not math.isfinite(xf) or xf < 0:
        raise MOSPError(
            f"{name} 必须是非负有限数，得到 {x!r}", status.INVALID_REQUEST
        )
    if xf > limits.max_weight:
        raise MOSPError(
            f"{name}={xf:g} 超过允许上限 {limits.max_weight:g}",
            status.LIMIT_EXCEEDED,
        )
    return xf


def _require_label_cap(req, limits: SolveLimits):
    if "label_cap" not in req or req["label_cap"] is None:
        return limits.default_label_cap
    x = req["label_cap"]
    if isinstance(x, bool) or not isinstance(x, int):
        raise MOSPError(
            f"label_cap 必须是正整数，得到 {x!r}", status.INVALID_REQUEST
        )
    if not (1 <= x <= limits.max_label_cap):
        raise MOSPError(
            f"label_cap 必须在 [1, {limits.max_label_cap}] 内，得到 {x}",
            status.LIMIT_EXCEEDED,
        )
    return x


def _require_eps(req):
    if "eps" not in req or req["eps"] is None:
        return DEFAULT_EPS
    x = req["eps"]
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise MOSPError(f"eps 必须是数字，得到 {x!r}", status.INVALID_REQUEST)
    xf = float(x)
    if not math.isfinite(xf) or not (MIN_EPS <= xf <= MAX_EPS):
        raise MOSPError(
            f"eps 必须在 [{MIN_EPS:g}, {MAX_EPS:g}] 内，得到 {x!r}",
            status.INVALID_REQUEST,
        )
    return xf


def encode_response(result, graph: Graph, parameters: dict) -> dict:
    """把 :class:`~mospp.solver.SolveResult` 编码为 JSON 可序列化的 dict。"""
    paths = []
    pareto = []
    for lab in result.target_labels:
        node_idx, arc_seq = lab.path()
        nodes = [graph.node_ids[i] for i in node_idx]
        edges = [
            {
                "from": graph.node_ids[a.u],
                "to": graph.node_ids[a.v],
                "time": a.time,
                "cost": a.cost,
                "key": a.key,
            }
            for a in arc_seq
        ]
        paths.append({"nodes": nodes, "edges": edges, "time": lab.time, "cost": lab.cost})
        pareto.append({"time": lab.time, "cost": lab.cost})
    return {
        "status": result.status,
        "paths": paths,
        "pareto": pareto,
        "stats": dict(result.stats),
        "parameters": parameters,
    }


def _error_response(code: str, message: str, parameters: dict | None = None) -> dict:
    return {
        "status": code,
        "error": message,
        "paths": [],
        "pareto": [],
        "stats": None,
        "parameters": parameters or {},
    }


def solve_request(req: dict) -> dict:
    """求解入口：吃一个 JSON 兼容 dict，吐一个 JSON 兼容 dict。"""
    limits = SolveLimits()
    params: dict = {}
    try:
        if not isinstance(req, dict):
            raise MOSPError("请求体必须是 JSON 对象", status.INVALID_REQUEST)

        eps = _require_eps(req)
        time_budget = _require_optional_number(req, "time_budget", limits)
        cost_budget = _require_optional_number(req, "cost_budget", limits)
        label_cap = _require_label_cap(req, limits)

        # 权重也必须落在输入范围内（在边读入前检查 max_weight）。
        graph = Graph.from_request(req, limits)
        for a in graph.arcs:
            if a.time > limits.max_weight or a.cost > limits.max_weight:
                raise MOSPError(
                    f"边权重超过允许上限 {limits.max_weight:g}："
                    f"{graph.node_ids[a.u]}->{graph.node_ids[a.v]} "
                    f"time={a.time:g}, cost={a.cost:g}",
                    status.LIMIT_EXCEEDED,
                )

        s = graph.index[req["source"]]
        t = graph.index[req["target"]]
        params = {
            "source": req["source"],
            "target": req["target"],
            "directed": bool(req.get("directed", True)),
            "time_budget": time_budget,
            "cost_budget": cost_budget,
            "label_cap": label_cap,
            "eps": eps,
        }

        solver = Solver(graph, eps=eps)
        result = solver.solve(
            source_idx=s,
            target_idx=t,
            time_budget=time_budget,
            cost_budget=cost_budget,
            label_cap=label_cap,
        )
        result.stats["label_cap"] = label_cap
        result.stats["nodes"] = graph.n
        result.stats["arcs"] = graph.m
        return encode_response(result, graph, params)

    except MOSPError as e:
        return _error_response(e.code, e.detail, params)
    except Exception as e:  # pragma: no cover - 兜底，避免把堆栈泄露给调用方
        return _error_response(
            status.INTERNAL_ERROR,
            f"内部错误: {type(e).__name__}: {e}",
            params,
        )
