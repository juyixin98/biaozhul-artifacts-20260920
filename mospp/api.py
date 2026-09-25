"""JSON 接口层：请求校验、求解、响应构造。

请求结构（所有未知字段一律拒绝）::

    {
      "graph": {
        "nodes": ["A", "B", ...],          # 节点 id：非空字符串或整数
        "edges": [
          {"source": "A", "target": "B",
           "time": 3.0, "cost": 5.0, "id": "e1"}  # id 可选
        ]
      },
      "source": "A",
      "target": "D",
      "mode": "simple",          # "simple"（默认）| "walk"
      "time_budget": 10.0,       # 可选，>= 0
      "cost_budget": 8.0,        # 可选，>= 0
      "node_label_cap": 10000,   # 可选
      "total_label_cap": 500000, # 可选
      "atol": 1e-9,              # 可选，[0, 1e-2]
      "rtol": 1e-9,              # 可选，[0, 1e-2]
      "verify_paths": true       # 可选，默认 true：重建后再次独立校验
    }

成功响应::

    {"status": "ok" | "no_path" | "truncated",
     "pareto_front": [{"time": ..., "cost": ...,
                       "nodes": [...], "edges": [...]}],
     "truncated": false,
     "truncation": [...],
     "statistics": {...}}

错误响应::

    {"error": "<code>", "message": "...", "field": "<json-pointer>"}
"""

from __future__ import annotations

import math
from typing import Any

from .errors import (
    DEFAULT_NODE_LABEL_CAP,
    DEFAULT_TOTAL_LABEL_CAP,
    MAX_ATOL,
    MAX_EDGES,
    MAX_ID_LEN,
    MAX_NODES,
    MAX_PARALLEL_EDGES,
    MAX_RTOL,
    MAX_WEIGHT,
    MIN_WEIGHT,
    MIN_ATOL,
    MIN_RTOL,
    SIMPLE_MODE_MAX_EDGES,
    SIMPLE_MODE_MAX_NODES,
    LimitExceeded,
    RequestError,
)
from .graph import Graph
from .labeling import solve, verify_path
from .tolerance import objectives_equal

_ALLOWED_TOP = {
    "graph",
    "source",
    "target",
    "mode",
    "time_budget",
    "cost_budget",
    "node_label_cap",
    "total_label_cap",
    "atol",
    "rtol",
    "verify_paths",
}
_ALLOWED_GRAPH = {"nodes", "edges"}
_ALLOWED_EDGE = {"source", "target", "time", "cost", "id"}


def _reject_unknown(obj: dict, allowed: set[str], prefix: str) -> None:
    for key in obj:
        if key not in allowed:
            raise RequestError(
                "unknown_field",
                f"未知字段 {key!r}，允许的字段：{sorted(allowed)}",
                f"{prefix}.{key}" if prefix else key,
            )


def _check_id(value: Any, field: str) -> str | int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise RequestError(
            "invalid_id",
            f"{field} 必须是非空字符串或整数，得到 {type(value).__name__}",
            field,
        )
    if isinstance(value, str):
        if not value or len(value) > MAX_ID_LEN:
            raise RequestError(
                "invalid_id",
                f"{field} 必须是非空、长度 <= {MAX_ID_LEN} 的字符串",
                field,
            )
    return value


def _check_weight(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestError(
            "invalid_weight",
            f"{field} 必须是数值，得到 {type(value).__name__}",
            field,
        )
    f = float(value)
    # NaN 与 ±Inf 都不是合法有限权重，统一报 invalid_weight
    if math.isnan(f) or math.isinf(f):
        raise RequestError(
            "invalid_weight", f"{field} 必须是有限数，得到 {value}", field
        )
    if f < MIN_WEIGHT - 0.0:
        raise RequestError(
            "negative_weight",
            f"{field} 必须非负，得到 {f}（本项目只支持非负时间/费用）",
            field,
        )
    if f > MAX_WEIGHT:
        raise RequestError(
            "invalid_weight",
            f"{field} 超过上限 {MAX_WEIGHT:g}",
            field,
        )
    return f


def _check_budget(value: Any, field: str) -> float:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestError(
            "invalid_budget",
            f"{field} 必须是非负数值，得到 {type(value).__name__}",
            field,
        )
    f = float(value)
    if math.isnan(f) or math.isinf(f) or f < 0.0:
        raise RequestError(
            "invalid_budget", f"{field} 必须是非负有限数", field
        )
    return f


def _check_tol(value: Any, field: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestError(
            "invalid_tolerance",
            f"{field} 必须是 [0, {hi:g}] 内的数值",
            field,
        )
    f = float(value)
    if math.isnan(f) or math.isinf(f) or not (lo <= f <= hi):
        raise RequestError(
            "invalid_tolerance",
            f"{field}={value} 超出允许范围 [{lo:g}, {hi:g}]",
            field,
        )
    return f


def _check_cap(value: Any, field: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RequestError(
            "invalid_cap", f"{field} 必须是 >= 1 的整数", field
        )
    return int(value)


def parse_request(req: Any) -> dict:
    """校验并规范化请求，返回求解所需的结构化参数。"""
    if not isinstance(req, dict):
        raise RequestError(
            "invalid_request", "请求体必须是 JSON 对象", None
        )
    _reject_unknown(req, _ALLOWED_TOP, "")

    if "graph" not in req:
        raise RequestError("missing_field", "缺少必填字段 graph", "graph")
    graph_obj = req["graph"]
    if not isinstance(graph_obj, dict):
        raise RequestError(
            "invalid_graph", "graph 必须是对象", "graph"
        )
    _reject_unknown(graph_obj, _ALLOWED_GRAPH, "graph")

    # ---- 节点 ----------------------------------------------------------
    if "nodes" not in graph_obj:
        raise RequestError(
            "missing_field", "缺少 graph.nodes", "graph.nodes"
        )
    raw_nodes = graph_obj["nodes"]
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise RequestError(
            "invalid_graph",
            "graph.nodes 必须是非空数组",
            "graph.nodes",
        )
    if len(raw_nodes) > MAX_NODES:
        raise LimitExceeded(
            f"节点数 {len(raw_nodes)} 超过硬上限 {MAX_NODES}",
            "graph.nodes",
        )
    nodes: list = []
    seen: set = set()
    for i, n in enumerate(raw_nodes):
        nid = _check_id(n, f"graph.nodes[{i}]")
        if nid in seen:
            raise RequestError(
                "duplicate_node",
                f"节点 id {nid!r} 重复",
                f"graph.nodes[{i}]",
            )
        seen.add(nid)
        nodes.append(nid)

    # ---- 边 ------------------------------------------------------------
    if "edges" not in graph_obj:
        raise RequestError(
            "missing_field", "缺少 graph.edges", "graph.edges"
        )
    raw_edges = graph_obj["edges"]
    if not isinstance(raw_edges, list):
        raise RequestError(
            "invalid_graph", "graph.edges 必须是数组", "graph.edges"
        )
    if len(raw_edges) > MAX_EDGES:
        raise LimitExceeded(
            f"边数 {len(raw_edges)} 超过硬上限 {MAX_EDGES}",
            "graph.edges",
        )
    id_to_idx = {n: i for i, n in enumerate(nodes)}
    pair_count: dict[tuple, int] = {}
    edges: list[dict] = []
    for i, e in enumerate(raw_edges):
        pfx = f"graph.edges[{i}]"
        if not isinstance(e, dict):
            raise RequestError(
                "invalid_edge", f"{pfx} 必须是对象", pfx
            )
        _reject_unknown(e, _ALLOWED_EDGE, pfx)
        for required in ("source", "target", "time", "cost"):
            if required not in e:
                raise RequestError(
                    "missing_field",
                    f"{pfx} 缺少字段 {required}",
                    f"{pfx}.{required}",
                )
        src = _check_id(e["source"], f"{pfx}.source")
        dst = _check_id(e["target"], f"{pfx}.target")
        if src not in id_to_idx:
            raise RequestError(
                "unknown_node",
                f"{pfx}.source 引用了未声明的节点 {src!r}",
                f"{pfx}.source",
            )
        if dst not in id_to_idx:
            raise RequestError(
                "unknown_node",
                f"{pfx}.target 引用了未声明的节点 {dst!r}",
                f"{pfx}.target",
            )
        t = _check_weight(e["time"], f"{pfx}.time")
        c = _check_weight(e["cost"], f"{pfx}.cost")
        eid = None
        if "id" in e and e["id"] is not None:
            eid = _check_id(e["id"], f"{pfx}.id")
        pair = (src, dst)
        pair_count[pair] = pair_count.get(pair, 0) + 1
        if pair_count[pair] > MAX_PARALLEL_EDGES:
            raise LimitExceeded(
                f"{src}->{dst} 的平行边超过 {MAX_PARALLEL_EDGES} 条",
                pfx,
            )
        edges.append(
            {"source": src, "target": dst, "time": t, "cost": c, "id": eid}
        )

    graph = Graph.build(nodes, edges)

    # ---- source / target ----------------------------------------------
    if "source" not in req:
        raise RequestError("missing_field", "缺少 source", "source")
    if "target" not in req:
        raise RequestError("missing_field", "缺少 target", "target")
    source = _check_id(req["source"], "source")
    target = _check_id(req["target"], "target")
    if source not in id_to_idx:
        raise RequestError(
            "unknown_node", f"source {source!r} 不在节点列表中", "source"
        )
    if target not in id_to_idx:
        raise RequestError(
            "unknown_node", f"target {target!r} 不在节点列表中", "target"
        )

    # ---- 可选参数 ------------------------------------------------------
    mode = req.get("mode", "simple")
    if mode not in ("simple", "walk"):
        raise RequestError(
            "invalid_mode",
            f"mode 必须是 'simple' 或 'walk'，得到 {mode!r}",
            "mode",
        )
    if mode == "simple" and (
        len(nodes) > SIMPLE_MODE_MAX_NODES or len(edges) > SIMPLE_MODE_MAX_EDGES
    ):
        raise LimitExceeded(
            f"simple 模式限定 <= {SIMPLE_MODE_MAX_NODES} 节点 / "
            f"<= {SIMPLE_MODE_MAX_EDGES} 边；更大的图请使用 mode='walk'",
            "mode",
        )

    atol = _check_tol(req.get("atol", 1e-9), "atol", MIN_ATOL, MAX_ATOL)
    rtol = _check_tol(req.get("rtol", 1e-9), "rtol", MIN_RTOL, MAX_RTOL)
    node_cap = _check_cap(
        req.get("node_label_cap"), "node_label_cap", DEFAULT_NODE_LABEL_CAP
    )
    total_cap = _check_cap(
        req.get("total_label_cap"), "total_label_cap", DEFAULT_TOTAL_LABEL_CAP
    )
    time_budget = _check_budget(req.get("time_budget"), "time_budget")
    cost_budget = _check_budget(req.get("cost_budget"), "cost_budget")

    verify_paths = req.get("verify_paths", True)
    if not isinstance(verify_paths, bool):
        raise RequestError(
            "invalid_request",
            "verify_paths 必须是布尔值",
            "verify_paths",
        )

    return {
        "graph": graph,
        "source_idx": id_to_idx[source],
        "target_idx": id_to_idx[target],
        "source": source,
        "target": target,
        "mode": mode,
        "atol": atol,
        "rtol": rtol,
        "node_label_cap": node_cap,
        "total_label_cap": total_cap,
        "time_budget": time_budget,
        "cost_budget": cost_budget,
        "verify_paths": verify_paths,
    }


def _num(x: float):
    """整数权重以整数输出，其余保持浮点，便于阅读。"""
    if float(x).is_integer():
        return int(x)
    return round(float(x), 12)


def run_request(req: Any) -> dict:
    """解析请求、求解并构造 JSON 可序列化的响应字典。"""
    params = parse_request(req)
    graph: Graph = params["graph"]
    s = params["source_idx"]
    t = params["target_idx"]
    atol = params["atol"]
    rtol = params["rtol"]

    result = solve(
        graph,
        s,
        t,
        mode=params["mode"],
        atol=atol,
        rtol=rtol,
        time_budget=params["time_budget"],
        cost_budget=params["cost_budget"],
        node_label_cap=params["node_label_cap"],
        total_label_cap=params["total_label_cap"],
    )

    front = []
    verify_ok = True
    for rank, entry in enumerate(result.entries):
        all_routes = [(entry.nodes, entry.edges)] + list(
            zip(entry.alt_nodes, entry.alt_edges)
        )
        if params["verify_paths"]:
            for nodes, es in all_routes:
                vt, vc = verify_path(graph, nodes, es, s, t)
                if not objectives_equal(
                    (vt, vc), (entry.time, entry.cost), atol, rtol
                ):
                    verify_ok = False
                # simple 模式路径不得有重复节点
                if params["mode"] == "simple" and len(set(nodes)) != len(nodes):
                    verify_ok = False

        def _route_payload(nodes, es):
            return {
                "nodes": [graph.raw_node(v) for v in nodes],
                "edges": [graph.edge_ref(e) for e in es],
            }

        item = {
            "rank": rank,
            "time": _num(entry.time),
            "cost": _num(entry.cost),
            **_route_payload(entry.nodes, entry.edges),
        }
        if entry.alt_nodes:
            item["equal_paths"] = [
                _route_payload(nodes, es)
                for nodes, es in zip(entry.alt_nodes, entry.alt_edges)
            ]
            item["equal_path_count"] = 1 + len(entry.alt_nodes)
        front.append(item)

    resp: dict = {
        "status": result.status,
        "request_echo": {
            "source": params["source"],
            "target": params["target"],
            "mode": params["mode"],
            "time_budget": params["time_budget"],
            "cost_budget": params["cost_budget"],
            "atol": atol,
            "rtol": rtol,
        },
        "pareto_front": front,
        "truncated": result.truncated,
        "truncation": [
            rec.to_dict(graph) for rec in result.truncation
        ],
        "statistics": {
            **result.stats,
            "pareto_front_size": len(front),
            "paths_verified": bool(params["verify_paths"]),
            "paths_verification_ok": verify_ok,
        },
    }
    return resp
