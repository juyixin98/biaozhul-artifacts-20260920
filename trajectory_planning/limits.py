"""动力学限值与拐角策略。

拐角速度上限有两种明确策略：
- ``stop``：内部节点（或显式停点）速度为 0；
- ``curvature``：按用户显式给定的曲率 kappa（或圆角半径 R=1/kappa），
  以横向加速度限制 v <= sqrt(a_lat / kappa) = sqrt(a_lat * R)。

折返尖点（转角接近 pi）在任何策略下都强制为停点。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from .path import Polyline, turning_angles

STRATEGY_STOP = "stop"
STRATEGY_CURVATURE = "curvature"
CORNER_STRATEGIES = (STRATEGY_STOP, STRATEGY_CURVATURE)
DEFAULT_CUSP_ANGLE_RAD = float(np.deg2rad(170.0))


@dataclass(frozen=True)
class Dynamics:
    """标量动力学约束（切向）。"""

    v_max: float
    a_max: float
    d_max: float
    a_lat_max: Optional[float]
    v_start: float
    v_end: float


@dataclass(frozen=True)
class CornerConfig:
    """拐角策略配置。

    Args:
        strategy: ``stop`` 或 ``curvature``。
        radius: 显式全局圆角半径 R（米），与 curvature 二选一。
        curvature: 显式全局曲率 kappa（1/米）。
        node_curvatures: 逐节点显式曲率（长度等于去重后节点数，
            内部节点之外的位置被忽略；None 表示该节点用全局值）。
        stop_nodes: 无论采用何种策略都必须静止通过的节点编号。
        cusp_angle: 超过该转角（弧度）视为折返尖点，强制停车。
    """

    strategy: str
    radius: Optional[float]
    curvature: Optional[float]
    node_curvatures: Optional[Sequence[Optional[float]]]
    stop_nodes: frozenset
    cusp_angle: float = DEFAULT_CUSP_ANGLE_RAD


def _node_kappa(index: int, config: CornerConfig) -> Optional[float]:
    """取某内部节点的显式曲率：逐节点覆盖优先，其次全局值。"""
    if config.node_curvatures is not None:
        kappa = config.node_curvatures[index]
        if kappa is not None:
            return float(kappa)
    if config.curvature is not None:
        return float(config.curvature)
    if config.radius is not None:
        return 1.0 / float(config.radius)
    return None


def _curvature_limit(
    index: int, delta: float, dynamics: Dynamics, config: CornerConfig
) -> tuple[float, float]:
    """返回 (速度上限, 使用的曲率)；配置缺失时抛 ValueError。"""
    if dynamics.a_lat_max is None or dynamics.a_lat_max <= 0.0:
        raise ValueError(
            "curvature 策略需要正的 a_lat_max（横向加速度上限）"
        )
    kappa = _node_kappa(index, config)
    if kappa is None:
        raise ValueError(
            "curvature 策略需要显式曲率：请提供 corner_radius、"
            "corner_curvature 或 node_curvatures"
        )
    if kappa <= 0.0:
        raise ValueError(f"节点 {index} 的曲率必须为正，实际为 {kappa}")
    return float(np.sqrt(dynamics.a_lat_max / kappa)), kappa


def _blend_fit_warning(
    index: int, delta: float, config: CornerConfig, lengths: np.ndarray
) -> Optional[str]:
    """半径 R 的内接圆角切深为 R*tan(delta/2)，超出相邻段长时告警。"""
    if config.radius is None or delta <= 0.0:
        return None
    blend = config.radius * float(np.tan(delta / 2.0))
    room = float(min(lengths[index - 1], lengths[index]))
    if blend > room:
        return (
            f"节点 {index}：圆角切深 {blend:.4g} 超过相邻段可用长度 "
            f"{room:.4g}，速度上限仍然生效，但该半径在几何上无法完整落地"
        )
    return None


def node_speed_bounds(
    polyline: Polyline, dynamics: Dynamics, config: CornerConfig
) -> tuple[np.ndarray, list, list]:
    """计算每个节点的速度上限。

    Returns:
        (bounds, corner_info, warnings)：
        bounds 形状 (m,)；corner_info 为每个内部节点记录判定依据。
    """
    points, lengths = polyline.points, polyline.lengths
    angles = turning_angles(points, lengths)
    bounds = np.full(polyline.n_nodes, dynamics.v_max)
    bounds[0] = min(dynamics.v_max, dynamics.v_start)
    bounds[-1] = min(dynamics.v_max, dynamics.v_end)

    corner_info: list = []
    warnings: list = []

    for i in range(1, polyline.n_nodes - 1):
        delta = float(angles[i - 1])
        entry = {
            "node": i,
            "turning_angle_deg": float(np.degrees(delta)),
            "strategy": config.strategy,
            "limit": dynamics.v_max,
            "kappa": None,
            "forced_stop_reason": None,
        }

        if i in config.stop_nodes:
            limit, reason = 0.0, "explicit_stop_node"
        elif config.strategy == STRATEGY_STOP:
            limit, reason = 0.0, "stop_strategy"
        elif delta >= config.cusp_angle:
            limit, reason = 0.0, "cusp"
            warnings.append(
                f"节点 {i} 转角 {np.degrees(delta):.1f} 度，按折返尖点强制停车"
            )
        else:
            limit, kappa = _curvature_limit(i, delta, dynamics, config)
            limit = min(limit, dynamics.v_max)
            entry["kappa"] = kappa
            warning = _blend_fit_warning(i, delta, config, lengths)
            if warning:
                warnings.append(warning)
            reason = None

        if reason is not None:
            entry["forced_stop_reason"] = reason
        entry["limit"] = float(limit)
        bounds[i] = limit
        corner_info.append(entry)

    return bounds, corner_info, warnings
