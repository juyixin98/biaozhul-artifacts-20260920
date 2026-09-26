"""节点速度前后向传播与单段梯形速度规划。

传播规则（沿弧长 s，最紧约束优先）：
    v^2(s+L) <= v^2(s) + 2 a_max L        （前向，加速能力）
    v^2(s)   <= v^2(s+L) + 2 d_max L      （后向，制动能力）
两次传播都与节点自身的拐角速度上限取最小值，取交集后即为
时间最优（bang-coast-bang）的节点速度序列。

单段时间由「梯形/三角形速度曲线」闭式求解，保证切向加速度
分别不超过 a_max（加速）与 d_max（制动）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# 判定巡航段存在 / 数值清零用的容差
_SPEED_EPS = 1e-9
_RADICAND_EPS = 1e-9


class InfeasibleTrajectory(ValueError):
    """给定速度、加速度/减速度与路径长度下不存在可行时间参数化。"""


@dataclass(frozen=True)
class SegmentPlan:
    """一个直线段的速度曲线参数（加速-巡航-制动）。"""

    index: int
    length: float
    v_enter: float
    v_exit: float
    v_peak: float
    t_acc: float
    t_coast: float
    t_dec: float
    s_acc: float
    s_coast: float
    s_dec: float
    duration: float
    t_start: float

    def to_dict(self) -> dict:
        return {
            "segment": self.index,
            "length": self.length,
            "v_enter": self.v_enter,
            "v_exit": self.v_exit,
            "v_peak": self.v_peak,
            "t_acc": self.t_acc,
            "t_coast": self.t_coast,
            "t_dec": self.t_dec,
            "duration": self.duration,
            "t_start": self.t_start,
        }


def _reachable_squared(v_sq: float, accel: float, length: float) -> float:
    """沿一段长度后可达的最大速度平方；v=0 或长度为 0 时安全。"""
    return max(v_sq, 0.0) + 2.0 * accel * max(length, 0.0)


def propagate_node_speeds(
    lengths: np.ndarray,
    corner_bounds: np.ndarray,
    a_max: float,
    d_max: float,
) -> np.ndarray:
    """前向 + 后向传播求交，返回各节点可行最大速度。

    零长段（length == 0）只做速度取小，不参与任何除法，
    其两侧节点速度一致。
    """
    n = corner_bounds.shape[0]
    speeds = np.array(corner_bounds, dtype=float, copy=True)

    # 前向传播：用前段节点速度限制后段节点
    for i in range(1, n):
        length = float(lengths[i - 1])
        reachable_sq = _reachable_squared(speeds[i - 1] ** 2, a_max, length)
        speeds[i] = min(speeds[i], float(np.sqrt(max(reachable_sq, 0.0))))

    # 后向传播：用后段节点速度限制前段节点（制动）
    for i in range(n - 2, -1, -1):
        length = float(lengths[i])
        reachable_sq = _reachable_squared(speeds[i + 1] ** 2, d_max, length)
        speeds[i] = min(speeds[i], float(np.sqrt(max(reachable_sq, 0.0))))

    # 零长段：强制两侧节点速度相同（取较小者）
    for i in range(n - 1):
        if lengths[i] == 0.0:
            shared = min(speeds[i], speeds[i + 1])
            speeds[i] = speeds[i + 1] = shared

    return speeds


def plan_segment(
    index: int,
    length: float,
    v_enter: float,
    v_exit: float,
    a_max: float,
    d_max: float,
    v_cap: float,
    t_start: float,
) -> SegmentPlan:
    """闭式求解单段「加速-巡航-制动」时间分配。

    Args:
        length: 段长（米）；允许为 0，此时不做除法，耗时为 0。
        v_enter / v_exit: 段两端速度（米/秒）。
        v_cap: 该段内的整体速度上限（如全局 v_max）。

    Raises:
        InfeasibleTrajectory: 参数非正、端点速度超界，或两端速度差
            在该段长度内无法由 a_max/d_max 实现。
    """
    if length < 0.0:
        raise InfeasibleTrajectory(f"段 {index} 长度为负: {length}")
    if a_max <= 0.0 or d_max <= 0.0:
        raise InfeasibleTrajectory("a_max 与 d_max 必须为正数")
    if v_enter < -_SPEED_EPS or v_exit < -_SPEED_EPS:
        raise InfeasibleTrajectory(
            f"段 {index} 出现负速度 {v_enter:.4g} -> {v_exit:.4g}"
        )
    v_enter = max(v_enter, 0.0)
    v_exit = max(v_exit, 0.0)
    if max(v_enter, v_exit) > v_cap + 1e-9:
        raise InfeasibleTrajectory(
            f"段 {index} 端点速度超过速度上限: "
            f"{max(v_enter, v_exit):.4g} > {v_cap:.4g}"
        )

    # 零长段：不进行任何除法
    if length == 0.0:
        if abs(v_enter - v_exit) > _SPEED_EPS:
            raise InfeasibleTrajectory(
                f"零长段 {index} 两端速度不一致: {v_enter:.4g} != {v_exit:.4g}"
            )
        return SegmentPlan(
            index=index, length=0.0, v_enter=v_enter, v_exit=v_exit,
            v_peak=v_enter, t_acc=0.0, t_coast=0.0, t_dec=0.0,
            s_acc=0.0, s_coast=0.0, s_dec=0.0, duration=0.0, t_start=t_start,
        )

    # 仅加速/仅制动所能达到的（无切换）峰值速度平方
    accel_only_sq = v_enter**2 + 2.0 * a_max * length
    brake_only_sq = v_exit**2 + 2.0 * d_max * length
    if brake_only_sq < v_enter**2 - _RADICAND_EPS:
        raise InfeasibleTrajectory(
            f"段 {index}：需要在 {length:.4g} m 内从 {v_enter:.4g} 减速到 "
            f"{v_exit:.4g}，超出减速度上限 {d_max:.4g} m/s^2"
        )
    if accel_only_sq < v_exit**2 - _RADICAND_EPS:
        raise InfeasibleTrajectory(
            f"段 {index}：需要在 {length:.4g} m 内从 {v_enter:.4g} 加速到 "
            f"{v_exit:.4g}，超出加速度上限 {a_max:.4g} m/s^2"
        )

    # 同时加速与制动（三角形切换）所能达到的峰值速度平方
    # v_peak^2 = (2L + v_e^2/a_d + v_x^2/a_a) / (1/a_a + 1/a_d)
    triangle_peak_sq = (
        2.0 * length + v_exit**2 / d_max + v_enter**2 / a_max
    ) / (1.0 / a_max + 1.0 / d_max)
    if triangle_peak_sq < -_RADICAND_EPS:
        raise InfeasibleTrajectory(
            f"段 {index} 无可行三角形速度曲线（峰值平方为负）"
        )
    triangle_peak_sq = max(triangle_peak_sq, 0.0)
    cap_sq = v_cap**2

    if triangle_peak_sq <= cap_sq + _RADICAND_EPS:
        # 三角形：无巡航段
        v_peak = float(np.sqrt(triangle_peak_sq))
        s_acc = max((v_peak**2 - v_enter**2) / (2.0 * a_max), 0.0)
        s_dec = max((v_peak**2 - v_exit**2) / (2.0 * d_max), 0.0)
        s_coast = max(length - s_acc - s_dec, 0.0)
        t_acc = (v_peak - v_enter) / a_max if v_peak > v_enter + _SPEED_EPS else 0.0
        t_dec = (v_peak - v_exit) / d_max if v_peak > v_exit + _SPEED_EPS else 0.0
        t_coast = 0.0
    else:
        # 梯形：加速到 v_cap，巡航，再制动
        v_peak = v_cap
        s_acc = (v_cap**2 - v_enter**2) / (2.0 * a_max)
        s_dec = (v_cap**2 - v_exit**2) / (2.0 * d_max)
        s_coast = length - s_acc - s_dec
        if s_coast < -1e-9:
            raise InfeasibleTrajectory(
                f"段 {index}：加速与制动距离之和 ({s_acc + s_dec:.4g}) "
                f"超过段长 ({length:.4g})"
            )
        s_coast = max(s_coast, 0.0)
        t_acc = (v_cap - v_enter) / a_max if v_cap > v_enter + _SPEED_EPS else 0.0
        t_dec = (v_cap - v_exit) / d_max if v_cap > v_exit + _SPEED_EPS else 0.0
        t_coast = s_coast / v_cap if v_cap > _SPEED_EPS else 0.0

    duration = t_acc + t_coast + t_dec
    return SegmentPlan(
        index=index, length=length, v_enter=v_enter, v_exit=v_exit,
        v_peak=v_peak, t_acc=t_acc, t_coast=t_coast, t_dec=t_dec,
        s_acc=s_acc, s_coast=s_coast, s_dec=s_dec,
        duration=duration, t_start=t_start,
    )


def plan_all_segments(
    lengths: np.ndarray,
    node_speeds: np.ndarray,
    a_max: float,
    d_max: float,
    v_cap: float,
) -> list[SegmentPlan]:
    """对全部段依次做时间参数化，累加起始时刻。"""
    plans: list[SegmentPlan] = []
    t_cursor = 0.0
    for i in range(lengths.shape[0]):
        plan = plan_segment(
            index=i,
            length=float(lengths[i]),
            v_enter=float(node_speeds[i]),
            v_exit=float(node_speeds[i + 1]),
            a_max=a_max,
            d_max=d_max,
            v_cap=v_cap,
            t_start=t_cursor,
        )
        plans.append(plan)
        t_cursor += plan.duration
    return plans
