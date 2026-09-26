"""JSON 请求/响应：离线计算入口。

请求格式（详见 ``examples/request_square.json``）::

    {
      "name": "可选名称",
      "options": {"max_iterations": 50, "tol_step": 1e-8, ...},
      "nodes": [
        {"id": 0, "pose": [x, y, theta], "fixed": true}
      ],
      "edges": [
        {"id": 0, "i": 0, "j": 1,
         "measurement": [dx, dy, dtheta],
         "info": [[...3x3...], [...], [...]]
            或 {"sigma_xy": 0.05, "sigma_theta": 0.035, "corr": 0.0},
         "kernel": {"type": "huber", "delta": 1.0}}
      ]
    }
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np

from .graph import PoseGraph, make_information_matrix
from .kernels import Kernel
from .optimizer import (
    GraphNotSolvedError,
    OptimizerOptions,
    OptimizeResult,
    optimize,
)


class ParseError(ValueError):
    """JSON 请求结构或数值不合法。"""


def _require(obj: dict, key: str, ctx: str) -> Any:
    if key not in obj:
        raise ParseError(f"{ctx} 缺少必填字段 {key!r}")
    return obj[key]


def _as_vec3(value: Any, ctx: str) -> np.ndarray:
    try:
        arr = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ParseError(f"{ctx} 必须是 3 个数字") from exc
    if arr.shape != (3,):
        raise ParseError(f"{ctx} 必须是长度为 3 的数组，实际形状 {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise ParseError(f"{ctx} 含非有限数值（NaN/Inf）")
    return arr


def _parse_info(value: Any, ctx: str) -> np.ndarray:
    if isinstance(value, dict):
        try:
            sigma_xy = float(value.get("sigma_xy", 0.05))
            sigma_theta = float(value.get("sigma_theta", 0.035))
            corr = float(value.get("corr", 0.0))
            return make_information_matrix(sigma_xy, sigma_theta, corr)
        except ValueError as exc:
            raise ParseError(f"{ctx} 参数非法: {exc}") from exc
    try:
        arr = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ParseError(f"{ctx} 必须是 3x3 数组或噪声参数对象") from exc
    if arr.shape != (3, 3):
        raise ParseError(f"{ctx} 必须是 3x3 矩阵，实际形状 {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise ParseError(f"{ctx} 含非有限数值（NaN/Inf）")
    if not np.allclose(arr, arr.T, atol=1e-8):
        raise ParseError(f"{ctx} 必须对称")
    eigvals = np.linalg.eigvalsh(arr)
    if eigvals.min() <= 0.0:
        raise ParseError(
            f"{ctx} 必须正定（最小特征值 {eigvals.min():.3e} <= 0）"
        )
    return arr


def _parse_kernel(value: Any, ctx: str) -> Kernel:
    if value is None:
        return Kernel("none")
    if not isinstance(value, dict):
        raise ParseError(f"{ctx} 必须是对象，例如 {{\"type\": \"huber\", \"delta\": 1.0}}")
    try:
        return Kernel(
            name=str(value.get("type", "none")),
            delta=float(value.get("delta", 1.0)),
        )
    except ValueError as exc:
        raise ParseError(f"{ctx} 配置非法: {exc}") from exc


def graph_from_dict(payload: dict) -> tuple[PoseGraph, OptimizerOptions]:
    """把已解析的 JSON 字典转换为位姿图与求解器选项。"""
    if not isinstance(payload, dict):
        raise ParseError("请求根对象必须是 JSON object")
    raw_nodes = _require(payload, "nodes", "请求")
    raw_edges = payload.get("edges", [])
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise ParseError("'nodes' 与 'edges' 必须是数组")
    if len(raw_nodes) == 0:
        raise ParseError("'nodes' 不能为空")

    graph = PoseGraph()
    seen_ids: set[int] = set()
    seen_edge_ids: set[int] = set()
    for k, raw in enumerate(raw_nodes):
        ctx = f"nodes[{k}]"
        if not isinstance(raw, dict):
            raise ParseError(f"{ctx} 必须是对象")
        nid = _require(raw, "id", ctx)
        if not isinstance(nid, int) or isinstance(nid, bool):
            raise ParseError(f"{ctx}.id 必须是整数")
        if nid in seen_ids:
            raise ParseError(f"节点 id {nid} 重复")
        seen_ids.add(nid)
        pose = _as_vec3(_require(raw, "pose", ctx), f"{ctx}.pose")
        graph.add_node(nid, pose, fixed=bool(raw.get("fixed", False)))

    for k, raw in enumerate(raw_edges):
        ctx = f"edges[{k}]"
        if not isinstance(raw, dict):
            raise ParseError(f"{ctx} 必须是对象")
        ei = _require(raw, "i", ctx)
        ej = _require(raw, "j", ctx)
        if not isinstance(ei, int) or isinstance(ei, bool):
            raise ParseError(f"{ctx}.i 必须是整数")
        if not isinstance(ej, int) or isinstance(ej, bool):
            raise ParseError(f"{ctx}.j 必须是整数")
        measurement = _as_vec3(
            _require(raw, "measurement", ctx), f"{ctx}.measurement"
        )
        info = _parse_info(_require(raw, "info", ctx), f"{ctx}.info")
        kernel = _parse_kernel(raw.get("kernel"), f"{ctx}.kernel")
        edge_id = raw.get("id", k)
        if not isinstance(edge_id, int) or isinstance(edge_id, bool):
            raise ParseError(f"{ctx}.id 必须是整数")
        if edge_id in seen_edge_ids:
            raise ParseError(f"边 id {edge_id} 跨类型重复（可能与自动编号冲突）")
        seen_edge_ids.add(edge_id)
        try:
            graph.add_edge(ei, ej, measurement, info, kernel, edge_id=edge_id)
        except ValueError as exc:
            raise ParseError(f"{ctx}: {exc}") from exc

    options = _parse_options(payload.get("options", {}))
    return graph, options


def _parse_options(value: Any) -> OptimizerOptions:
    if not isinstance(value, dict):
        raise ParseError("'options' 必须是对象")
    allowed = {
        "max_iterations": int,
        "tol_step": float,
        "tol_cost": float,
        "initial_lambda": float,
        "verbose": bool,
    }
    kwargs: dict[str, Any] = {}
    for key, caster in allowed.items():
        if key in value:
            try:
                kwargs[key] = caster(value[key])
            except (TypeError, ValueError) as exc:
                raise ParseError(f"options.{key} 类型非法") from exc
    try:
        return OptimizerOptions(**kwargs)
    except ValueError as exc:
        raise ParseError(f"options 非法: {exc}") from exc


def load_request(path: str) -> tuple[PoseGraph, OptimizerOptions, str]:
    """从 JSON 文件读取请求。返回 (图, 选项, 名称)。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError as exc:
        raise ParseError(f"请求文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ParseError(f"JSON 解析失败（{exc.msg}，第 {exc.lineno} 行）") from exc
    graph, options = graph_from_dict(payload)
    return graph, options, str(payload.get("name", ""))


def result_to_dict(
    result: OptimizeResult,
    graph: PoseGraph,
    name: str = "",
    problems: list[str] | None = None,
) -> dict:
    """把优化结果序列化为可 JSON 化的字典。"""
    poses_out = [
        {
            "id": nid,
            "pose": [float(v) for v in result.poses[nid]],
            "fixed": graph.nodes[nid].fixed,
        }
        for nid in graph.ordered_ids
    ]
    edges_out = [
        {
            "id": edge.id,
            "i": edge.i,
            "j": edge.j,
            "kernel": edge.kernel.name,
            "chi2": float(result.edge_chi2.get(edge.id, 0.0)),
            "robust_weight": float(result.weights.get(edge.id, 1.0)),
        }
        for edge in sorted(graph.edges, key=lambda e: e.id)
    ]
    return {
        "name": name,
        "success": result.success,
        "summary": result.to_summary(),
        "poses": poses_out,
        "edges": edges_out,
        "cost_history": [float(c) for c in result.cost_history],
        "diagnostics": {
            "components": [
                {"size": len(c), "node_ids": [int(x) for x in c]}
                for c in result.components
            ],
            "problems": problems or [],
            "note": "非线性最小二乘仅保证局部收敛，不承诺全局最优",
        },
    }


def graph_error_to_dict(
    graph: PoseGraph | None, problems: list[str], name: str = ""
) -> dict:
    """图结构无法求解时的诊断响应（HTTP 语义上的 422 类错误，但本库离线运行）。"""
    components: list[list[int]] = []
    poses_out: list[dict] = []
    if graph is not None and graph.nodes:
        from .graph import connected_components

        components = connected_components(graph.ordered_ids, graph.edges)
        poses_out = [
            {
                "id": nid,
                "pose": [float(v) for v in node.initial_pose],
                "fixed": node.fixed,
            }
            for nid, node in sorted(graph.nodes.items())
        ]
    return {
        "name": name,
        "success": False,
        "summary": {
            "success": False,
            "status": "graph_error",
            "message": "；".join(problems),
        },
        "poses": poses_out,
        "edges": [],
        "cost_history": [],
        "diagnostics": {
            "components": [
                {"size": len(c), "node_ids": [int(x) for x in c]}
                for c in components
            ],
            "problems": problems,
            "note": "非线性最小二乘仅保证局部收敛，不承诺全局最优",
        },
    }


def dump_response(response: dict, path: str | None = None) -> str:
    """序列化响应；``path`` 给定时写文件，同时返回 JSON 字符串。"""
    text = json.dumps(response, ensure_ascii=False, indent=2)
    if path is not None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return text


def run_request_dict(payload: dict) -> dict:
    """直接对内存中的请求字典执行完整流程（便于程序化调用）。"""
    graph, options = graph_from_dict(payload)
    name = str(payload.get("name", ""))
    try:
        result = optimize(graph, options)
    except GraphNotSolvedError as exc:
        return graph_error_to_dict(graph, [str(exc)], name)
    return result_to_dict(result, graph, name)
