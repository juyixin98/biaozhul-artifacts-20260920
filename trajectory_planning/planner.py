"""高层规划入口：请求解析 -> 拐角限值 -> 传播 -> 分段 -> 档案 -> 校验。"""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from .kinematics import (
    InfeasibleTrajectory,
    plan_all_segments,
    propagate_node_speeds,
)
from .limits import (
    CORNER_STRATEGIES,
    DEFAULT_CUSP_ANGLE_RAD,
    STRATEGY_STOP,
    CornerConfig,
    Dynamics,
    node_speed_bounds,
)
from .path import Polyline, build_polyline
from .profile import TrajectoryProfile, validate_profile


def _require_positive(name: str, value: float) -> float:
    value = float(value)
    if not np.isfinite(value) or value <= 0.0:
        raise ValueError(f"{name} 必须为正数，实际为 {value}")
    return value


def _require_nonneg(name: str, value: float) -> float:
    value = float(value)
    if not np.isfinite(value) or value < 0.0:
        raise ValueError(f"{name} 必须为非负数，实际为 {value}")
    return value


def parse_dynamics(payload: dict) -> Dynamics:
    """解析并校验动力学参数。"""
    v_max = _require_positive("v_max", payload["v_max"])
    a_max = _require_positive("a_max", payload["a_max"])
    d_max = _require_positive("d_max", payload.get("d_max", payload["a_max"]))
    v_start = _require_nonneg("v_start", payload.get("v_start", 0.0))
    v_end = _require_nonneg("v_end", payload.get("v_end", 0.0))
    if v_start > v_max:
        raise ValueError(f"v_start ({v_start}) 不能超过 v_max ({v_max})")
    if v_end > v_max:
        raise ValueError(f"v_end ({v_end}) 不能超过 v_max ({v_max})")

    a_lat_max = payload.get("a_lat_max")
    if a_lat_max is not None:
        a_lat_max = _require_positive("a_lat_max", a_lat_max)
    return Dynamics(
        v_max=v_max, a_max=a_max, d_max=d_max,
        a_lat_max=a_lat_max, v_start=v_start, v_end=v_end,
    )


def parse_corner_config(payload: dict, n_nodes: int) -> CornerConfig:
    """解析拐角策略，逐节点曲率/半径做长度与取值校验。"""
    strategy = payload.get("strategy", STRATEGY_STOP)
    if strategy not in CORNER_STRATEGIES:
        raise ValueError(
            f"未知 corner.strategy={strategy!r}，可选 {CORNER_STRATEGIES}"
        )

    radius = payload.get("radius")
    curvature = payload.get("curvature")
    if radius is not None:
        radius = _require_positive("corner.radius", radius)
    if curvature is not None:
        curvature = _require_positive("corner.curvature", curvature)
    if radius is not None and curvature is not None:
        raise ValueError("corner.radius 与 corner.curvature 只能提供一个")

    node_curvatures: Optional[Sequence[Optional[float]]] = None
    raw_node = payload.get("node_curvatures")
    if raw_node is not None:
        if len(raw_node) != n_nodes:
            raise ValueError(
                f"node_curvatures 长度 {len(raw_node)} 与节点数 {n_nodes} 不一致"
            )
        node_curvatures = []
        for i, value in enumerate(raw_node):
            if value is None:
                node_curvatures.append(None)
            else:
                node_curvatures.append(_require_positive(
                    f"node_curvatures[{i}]", value
                ))

    stop_nodes = payload.get("stop_nodes", [])
    stop_set = frozenset(int(i) for i in stop_nodes)
    bad = [i for i in stop_set if i < 0 or i >= n_nodes]
    if bad:
        raise ValueError(f"stop_nodes 含越界编号: {sorted(bad)}")

    cusp_angle = float(
        payload.get("cusp_angle_deg", np.degrees(DEFAULT_CUSP_ANGLE_RAD))
    )
    if not 0.0 < cusp_angle <= 180.0:
        raise ValueError("cusp_angle_deg 必须在 (0, 180] 范围内")

    return CornerConfig(
        strategy=strategy,
        radius=radius,
        curvature=curvature,
        node_curvatures=node_curvatures,
        stop_nodes=stop_set,
        cusp_angle=float(np.deg2rad(cusp_angle)),
    )


def build_profile(
    raw_points,
    dynamics_payload: dict,
    corner_payload: Optional[dict] = None,
    validate_dt: float = 1e-3,
) -> tuple[TrajectoryProfile, dict]:
    """执行完整规划并数值校验，返回 (档案, 校验报告)。"""
    polyline = build_polyline(raw_points)
    dynamics = parse_dynamics(dynamics_payload)
    corner_payload = corner_payload or {}
    config = parse_corner_config(corner_payload, polyline.n_nodes)

    corner_bounds, corner_info, warnings = node_speed_bounds(
        polyline, dynamics, config
    )
    node_speeds = propagate_node_speeds(
        polyline.lengths, corner_bounds, dynamics.a_max, dynamics.d_max
    )

    # 起/终速度是等式约束：传播若把它们压低，说明路径太短无法在
    # d_max/a_max 内满足用户指定速度，必须报不可行，禁止静默篡改。
    _FEAS_EPS = 1e-6
    if node_speeds[0] + _FEAS_EPS < dynamics.v_start:
        raise InfeasibleTrajectory(
            f"起点速度 {dynamics.v_start:.4g} m/s 不可行：路径长度 "
            f"{polyline.total_length:.4g} m 无法在减速度上限 "
            f"{dynamics.d_max:.4g} m/s^2 内安全到达终点速度 "
            f"{dynamics.v_end:.4g} m/s（起点最多允许 "
            f"{node_speeds[0]:.4g} m/s）"
        )
    if node_speeds[-1] + _FEAS_EPS < dynamics.v_end:
        raise InfeasibleTrajectory(
            f"终点速度 {dynamics.v_end:.4g} m/s 不可行：路径长度 "
            f"{polyline.total_length:.4g} m 无法在加速度上限 "
            f"{dynamics.a_max:.4g} m/s^2 内从起点速度 "
            f"{dynamics.v_start:.4g} m/s 达到该速度（终点最多允许 "
            f"{node_speeds[-1]:.4g} m/s）"
        )

    try:
        segments = plan_all_segments(
            polyline.lengths, node_speeds,
            dynamics.a_max, dynamics.d_max, dynamics.v_max,
        )
    except InfeasibleTrajectory:
        raise

    total_time = float(sum(seg.duration for seg in segments))
    profile = TrajectoryProfile(
        polyline=polyline,
        lengths=polyline.lengths,
        node_speeds=node_speeds,
        corner_bounds=corner_bounds,
        segments=tuple(segments),
        total_time=total_time,
        corner_info=tuple(corner_info),
        warnings=tuple(warnings),
    )
    report = validate_profile(
        profile,
        v_max=dynamics.v_max,
        a_max=dynamics.a_max,
        d_max=dynamics.d_max,
        v_start_req=dynamics.v_start,
        v_end_req=dynamics.v_end,
        dt=validate_dt,
    )
    return profile, report


def plan_from_request(request: dict) -> dict:
    """解析完整 JSON 请求并返回可序列化的规划结果（不含稠密采样）。"""
    if not isinstance(request, dict):
        raise ValueError("请求必须是 JSON 对象")
    if "points" not in request:
        raise ValueError("请求缺少 points 字段")

    profile, report = build_profile(
        request["points"],
        request.get("dynamics", {}),
        request.get("corner"),
        validate_dt=float(request.get("validate_dt", 1e-3)),
    )
    return {
        "status": "ok",
        "summary": {
            "n_nodes_input": len(request["points"]),
            "removed_duplicate_nodes": profile.polyline.removed_duplicates,
            "n_nodes": profile.n_nodes,
            "n_segments": len(profile.segments),
            "path_length": float(profile.polyline.total_length),
            "total_time": profile.total_time,
            "start_speed": profile.start_speed,
            "end_speed": profile.end_speed,
        },
        "node_speeds": [float(v) for v in profile.node_speeds],
        "corner_speed_bounds": [float(v) for v in profile.corner_bounds],
        "corner_details": list(profile.corner_info),
        "segments": [seg.to_dict() for seg in profile.segments],
        "validation": report,
        "warnings": list(profile.warnings),
    }
