"""JSON 入口：请求解析、校验与响应封装。

请求支持两种等价的运动体描述（可混用）：

1. 直接运动学参数
   {"center": [x, y], "velocity": [vx, vy], "radius": r}
2. 合成传感器采样（最小二乘拟合为线性轨迹）
   {"radius": r, "samples": [[t, x, y], ...]}

顶层结构：
{
  "time_window": [t_start, t_end],          // 可选，默认 [0.0, 1.0]
  "robot": <body>,
  "obstacles": [<body>, ...]
}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence, Union

import numpy as np

from .collision import ContactInterval, analyze_request, earliest_collision
from .models import CircleBody
from .trajectory import fit_linear_trajectory


class CollisionRequest:
    """已解析的碰撞分析请求（不可变值对象）。"""

    def __init__(
        self,
        robot: CircleBody,
        obstacles: Sequence[CircleBody],
        t_start: float = 0.0,
        t_end: float = 1.0,
    ) -> None:
        self.robot = robot
        self.obstacles: List[CircleBody] = list(obstacles)
        self.t_start = float(t_start)
        self.t_end = float(t_end)


def _require(obj: dict, key: str, ctx: str):
    if key not in obj:
        raise ValueError(f"{ctx} 缺少必填字段 '{key}'")
    return obj[key]


def _as_vec2(value, ctx: str) -> np.ndarray:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{ctx} 必须是长度为 2 的数组")
    try:
        vec = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{ctx} 必须为数值") from exc
    if not np.isfinite(vec).all():
        raise ValueError(f"{ctx} 必须为有限实数")
    return vec


def _parse_body(obj: dict, ctx: str) -> CircleBody:
    """解析单个运动体；直接参数与采样二选一。"""
    if not isinstance(obj, dict):
        raise ValueError(f"{ctx} 必须是 JSON 对象")
    radius = float(_require(obj, "radius", ctx))
    if radius < 0.0:
        raise ValueError(f"{ctx} 的 radius 不得为负")

    has_kin = "center" in obj or "velocity" in obj
    has_samples = "samples" in obj

    if has_kin and has_samples:
        raise ValueError(f"{ctx} 不能同时提供运动学参数与采样数据")

    if has_kin:
        center = _as_vec2(_require(obj, "center", ctx), f"{ctx}.center")
        velocity = _as_vec2(_require(obj, "velocity", ctx), f"{ctx}.velocity")
        return CircleBody(center=center, velocity=velocity, radius=radius)

    if has_samples:
        samples = obj["samples"]
        if not isinstance(samples, list) or len(samples) < 2:
            raise ValueError(f"{ctx}.samples 至少需要 2 个 [t, x, y] 采样点")
        try:
            arr = np.asarray(samples, dtype=float)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{ctx}.samples 必须为数值数组") from exc
        return fit_linear_trajectory(arr, radius)

    raise ValueError(f"{ctx} 必须提供 (center, velocity) 或 samples")


def parse_request(data: dict) -> CollisionRequest:
    """把已反序列化的字典校验并解析为 CollisionRequest。"""
    if not isinstance(data, dict):
        raise ValueError("请求顶层必须是 JSON 对象")

    t_start, t_end = 0.0, 1.0
    if "time_window" in data:
        window = data["time_window"]
        if not isinstance(window, (list, tuple)) or len(window) != 2:
            raise ValueError("time_window 必须是 [t_start, t_end]")
        t_start, t_end = float(window[0]), float(window[1])
    if not (np.isfinite(t_start) and np.isfinite(t_end)) or t_end < t_start:
        raise ValueError("time_window 必须为有限端点且 t_end >= t_start")

    robot = _parse_body(_require(data, "robot", "请求"), "robot")

    obstacles_raw = _require(data, "obstacles", "请求")
    if not isinstance(obstacles_raw, list):
        raise ValueError("obstacles 必须是数组")
    obstacles = [
        _parse_body(raw, f"obstacles[{i}]") for i, raw in enumerate(obstacles_raw)
    ]

    return CollisionRequest(robot, obstacles, t_start, t_end)


def load_request(path: Union[str, Path]) -> CollisionRequest:
    """从 JSON 文件加载并校验请求。"""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return parse_request(data)


def build_response(request: CollisionRequest) -> dict:
    """执行分析并生成统一响应信封。"""
    results = analyze_request(
        request.robot, request.obstacles, request.t_start, request.t_end
    )
    first = earliest_collision(results)

    return {
        "success": True,
        "data": {
            "time_window": [request.t_start, request.t_end],
            "robot": {
                "center": request.robot.center.tolist(),
                "velocity": request.robot.velocity.tolist(),
                "radius": request.robot.radius,
            },
            "obstacles": [
                {
                    "index": i,
                    "center": ob.center.tolist(),
                    "velocity": ob.velocity.tolist(),
                    "radius": ob.radius,
                    "analysis": results[i].as_dict(),
                }
                for i, ob in enumerate(request.obstacles)
            ],
            "earliest_collision": (
                None if first is None else
                {
                    "obstacle_index": results.index(first),
                    "t_enter": first.t_enter,
                    "kind": first.kind,
                }
            ),
            "any_collision": first is not None,
        },
        "error": None,
        "metadata": {
            "obstacle_count": len(request.obstacles),
            "method": "analytic_quadratic_roots",
            "interval_rule": "closed [t_enter, t_exit]; tangent and initial "
                             "overlap count as collision; no time sampling",
        },
    }


def build_error_response(message: str) -> dict:
    return {"success": False, "data": None, "error": message, "metadata": None}
