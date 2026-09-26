"""JSON 数据结构解析与结果序列化（库与外部之间的边界，全部做输入校验）。

请求 JSON 示例：
{
  "default_max_gap": 0.5,
  "edges": [
    {
      "parent": "base", "child": "link1", "type": "timed", "max_gap": 0.2,
      "keyframes": [
        {"t": 0.0, "translation": [0, 0, 0],
         "rotation": {"quaternion": [1, 0, 0, 0]}},
        {"t": 1.0, "translation": [0, 0, 1],
         "rotation": {"axis_angle": {"axis": [0, 0, 1], "angle": 1.5708}}}
      ]
    },
    {
      "parent": "link1", "child": "tool", "type": "static",
      "transform": {"translation": [0, 0, 0.1]}
    }
  ],
  "queries": [
    {"source": "tool", "target": "base", "time": 0.5}
  ]
}

rotation 支持三种写法（缺省为单位旋转）：
- {"quaternion": [w, x, y, z]}
- {"matrix": [[...3x3...]]}
- {"axis_angle": {"axis": [x, y, z], "angle": 弧度}}
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np

from .errors import InvalidRequestError, InvalidTransformError
from .timed_sequence import Keyframe, StaticTransformProvider, TimedTransformSequence
from .transform import Transform
from .tree import TransformTree

_ROTATION_KEYS = ("quaternion", "matrix", "axis_angle")


def _require(obj: Any, key: str, ctx: str) -> Any:
    if not isinstance(obj, dict) or key not in obj:
        raise InvalidRequestError(f"{ctx} 中缺少必需字段 '{key}'")
    return obj[key]


def _as_vec3(value: Any, ctx: str) -> np.ndarray:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise InvalidRequestError(f"{ctx} 必须是长度为 3 的数组")
    try:
        arr = np.asarray(value, dtype=float).reshape(3)
    except (TypeError, ValueError) as exc:
        raise InvalidRequestError(f"{ctx} 必须全部是数值") from exc
    if not np.all(np.isfinite(arr)):
        raise InvalidRequestError(f"{ctx} 必须全部是有限数值")
    return arr


def parse_rotation(spec: Any, ctx: str) -> np.ndarray | None:
    """解析旋转描述，返回 3x3 旋转矩阵；None 表示单位旋转。"""
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise InvalidRequestError(f"{ctx} 的 rotation 必须是对象")
    keys = [k for k in _ROTATION_KEYS if k in spec]
    if not keys:
        raise InvalidRequestError(f"{ctx} 的 rotation 必须是 quaternion / matrix / axis_angle 之一")
    if len(keys) > 1:
        raise InvalidRequestError(f"{ctx} 的 rotation 只能指定一种表示，发现 {keys}")
    kind = keys[0]
    try:
        if kind == "quaternion":
            q = spec["quaternion"]
            if not isinstance(q, (list, tuple)) or len(q) != 4:
                raise InvalidRequestError(f"{ctx} 四元数必须是长度为 4 的数组 [w,x,y,z]")
            return Transform.from_quaternion(np.asarray(q, dtype=float)).rotation
        if kind == "matrix":
            m = spec["matrix"]
            if not isinstance(m, list) or len(m) != 3 or not all(isinstance(row, list) and len(row) == 3 for row in m):
                raise InvalidRequestError(f"{ctx} 旋转矩阵必须是 3x3 二维数组")
            return np.asarray(m, dtype=float)
        aa = spec["axis_angle"]
        if not isinstance(aa, dict) or "axis" not in aa or "angle" not in aa:
            raise InvalidRequestError(f"{ctx} axis_angle 需要 axis 与 angle 字段")
        axis = _as_vec3(aa["axis"], f"{ctx} axis_angle.axis")
        angle = aa["angle"]
        if not isinstance(angle, (int, float)) or not np.isfinite(float(angle)):
            raise InvalidRequestError(f"{ctx} axis_angle.angle 必须是有限数值")
        norm = float(np.linalg.norm(axis))
        if norm < 1e-15:
            raise InvalidTransformError(f"{ctx} axis_angle.axis 不能是零向量")
        axis = axis / norm
        x, y, z = axis
        c, s = np.cos(angle), np.sin(angle)
        c_mat = np.array(
            [
                [c + x * x * (1 - c), x * y * (1 - c) - z * s, x * z * (1 - c) + y * s],
                [y * x * (1 - c) + z * s, c + y * y * (1 - c), y * z * (1 - c) - x * s],
                [z * x * (1 - c) - y * s, z * y * (1 - c) + x * s, c + z * z * (1 - c)],
            ]
        )
        return c_mat
    except InvalidRequestError:
        raise
    except InvalidTransformError:
        raise
    except (TypeError, ValueError) as exc:
        raise InvalidRequestError(f"{ctx} 旋转描述无法解析：{exc}") from exc


def parse_pose(spec: Any, ctx: str) -> Transform:
    """解析 {'translation': [...], 'rotation': {...}}（两者均可缺省）。"""
    if spec is None:
        spec = {}
    if not isinstance(spec, dict):
        raise InvalidRequestError(f"{ctx} 必须是对象")
    translation = _as_vec3(spec["translation"], f"{ctx}.translation") if "translation" in spec else np.zeros(3)
    rotation = parse_rotation(spec.get("rotation"), ctx)
    return Transform(rotation, translation)


def _parse_max_gap(value: Any, ctx: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not np.isfinite(float(value)) or float(value) <= 0:
        raise InvalidRequestError(f"{ctx} 必须是正数")
    return float(value)


def build_tree(request: dict) -> tuple[TransformTree, float | None]:
    """从请求字典构建变换树，返回 (树, 全局限时缺口) 。"""
    if not isinstance(request, dict):
        raise InvalidRequestError("请求根必须是 JSON 对象")
    default_max_gap = _parse_max_gap(request.get("default_max_gap"), "default_max_gap")
    tree = TransformTree()
    for frame in request.get("frames", []):
        if not isinstance(frame, str) or not frame:
            raise InvalidRequestError("frames 中的坐标系名称必须是非空字符串")
        tree.add_frame(frame)
    edges = request.get("edges", [])
    if not isinstance(edges, list):
        raise InvalidRequestError("edges 必须是数组")
    for i, edge in enumerate(edges):
        ctx = f"edges[{i}]"
        parent = _require(edge, "parent", ctx)
        child = _require(edge, "child", ctx)
        if not isinstance(parent, str) or not isinstance(child, str):
            raise InvalidRequestError(f"{ctx} 的 parent/child 必须是字符串")
        edge_type = edge.get("type", "timed")
        edge_gap = _parse_max_gap(edge.get("max_gap"), f"{ctx}.max_gap")
        effective_gap = edge_gap if edge_gap is not None else default_max_gap
        if edge_type == "static":
            provider = StaticTransformProvider(parse_pose(edge.get("transform"), f"{ctx}.transform"))
        elif edge_type == "timed":
            kf_specs = _require(edge, "keyframes", f"{ctx}(type=timed)")
            if not isinstance(kf_specs, list) or not kf_specs:
                raise InvalidRequestError(f"{ctx}.keyframes 必须是非空数组")
            keyframes = []
            for j, kf in enumerate(kf_specs):
                t = _require(kf, "t", f"{ctx}.keyframes[{j}]")
                if not isinstance(t, (int, float)) or isinstance(t, bool) or not np.isfinite(float(t)):
                    raise InvalidRequestError(f"{ctx}.keyframes[{j}].t 必须是有限数值")
                keyframes.append(Keyframe(float(t), parse_pose(kf, f"{ctx}.keyframes[{j}]")))
            provider = TimedTransformSequence(keyframes, max_gap=effective_gap)
        else:
            raise InvalidRequestError(f"{ctx}.type 只能是 'timed' 或 'static'，实际为 {edge_type!r}")
        tree.add_edge(parent, child, provider)
    return tree, default_max_gap


def parse_query(spec: Any, index: int, default_max_gap: float | None) -> dict:
    ctx = f"queries[{index}]"
    if not isinstance(spec, dict):
        raise InvalidRequestError(f"{ctx} 必须是对象")
    source = _require(spec, "source", ctx)
    target = _require(spec, "target", ctx)
    time_value = _require(spec, "time", ctx)
    if not isinstance(source, str) or not isinstance(target, str):
        raise InvalidRequestError(f"{ctx} 的 source/target 必须是字符串")
    if not isinstance(time_value, (int, float)) or isinstance(time_value, bool) or not np.isfinite(float(time_value)):
        raise InvalidRequestError(f"{ctx}.time 必须是有限数值")
    max_gap = _parse_max_gap(spec.get("max_gap"), f"{ctx}.max_gap")
    if max_gap is None:
        max_gap = default_max_gap
    return {"source": source, "target": target, "time": float(time_value), "max_gap": max_gap}


def serialize_transform(transform: Transform) -> dict:
    """把变换序列化为 JSON 友好结构（矩阵 + 四元数 + 平移都给出，便于核对）。"""
    return {
        "translation": [float(x) for x in transform.translation],
        "rotation_matrix": [[float(x) for x in row] for row in transform.rotation],
        "quaternion_wxyz": [float(x) for x in transform.to_quaternion()],
        "matrix4": [[float(x) for x in row] for row in transform.to_matrix()],
    }


def load_request(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise InvalidRequestError(f"无法读取请求文件：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise InvalidRequestError(f"JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}") from exc
    return data
