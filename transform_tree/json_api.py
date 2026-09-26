"""JSON 请求/响应入口：纯函数 handle_request(dict) -> dict。

请求格式：
{
  "transforms": [
    {"parent": "world", "child": "base", "time": 0.0,
     "translation": [x, y, z], "rotation_xyzw": [x, y, z, w]}
  ],
  "queries": [
    {"target": "world", "source": "tool", "time": 1.5}
  ]
}

响应格式：
{
  "ok": true,
  "frames": ["base", "tool", "world"],
  "results": [
    {"ok": true, "target": "world", "source": "tool", "time": 1.5,
     "translation": [...], "rotation_xyzw": [...], "matrix": [[...]]},
    {"ok": false, "target": ..., "source": ..., "time": ...,
     "error": {"type": "ExtrapolationError", "message": "..."}}
  ]
}

加载 transforms 阶段若出现拓扑错误（环、多父）或数据非法，
整体返回 {"ok": false, "error": {...}}；查询阶段的错误按条返回。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .exceptions import TransformTreeError
from .transform import Transform
from .tree import TransformTree


def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    """处理一个请求字典，返回响应字典（可 JSON 序列化）。"""
    if not isinstance(request, dict):
        return _top_error("ValueError", "请求必须是 JSON 对象")
    tree = TransformTree()
    try:
        _load_transforms(tree, request.get("transforms", []))
    except (TransformTreeError, ValueError, TypeError, KeyError) as exc:
        return _top_error(type(exc).__name__, str(exc))

    results = [
        _run_query(tree, query) for query in request.get("queries", [])
    ]
    return {
        "ok": True,
        "frames": tree.frames(),
        "results": results,
    }


def _load_transforms(tree: TransformTree, items: Any) -> None:
    if not isinstance(items, list):
        raise ValueError("'transforms' 必须是数组")
    for i, item in enumerate(items):
        parent = item["parent"]
        child = item["child"]
        time = float(item["time"])
        transform = Transform(
            translation=np.asarray(item["translation"], dtype=float),
            rotation=np.asarray(item["rotation_xyzw"], dtype=float),
        )
        try:
            tree.add_transform(parent, child, time, transform)
        except TransformTreeError as exc:
            raise type(exc)(f"transforms[{i}]: {exc}") from exc


def _run_query(tree: TransformTree, query: Any) -> dict[str, Any]:
    base = {"target": None, "source": None, "time": None}
    if isinstance(query, dict):
        base = {
            "target": query.get("target"),
            "source": query.get("source"),
            "time": query.get("time"),
        }
    try:
        if not isinstance(query, dict):
            raise ValueError("query 必须是对象")
        transform = tree.lookup_transform(
            base["target"], base["source"], float(base["time"])
        )
    except (TransformTreeError, ValueError, TypeError) as exc:
        return {
            "ok": False,
            **base,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
    return {
        "ok": True,
        **base,
        "translation": [float(v) for v in transform.translation],
        "rotation_xyzw": [float(v) for v in transform.rotation],
        "matrix": [[float(v) for v in row] for row in transform.as_matrix()],
    }


def _top_error(error_type: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"type": error_type, "message": message}}
