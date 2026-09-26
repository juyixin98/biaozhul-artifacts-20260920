"""轨迹档案：按时间/弧长采样、位置与速度加速度、数值校验。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .kinematics import SegmentPlan
from .path import Polyline

_NUMERIC_EPS = 1e-7


@dataclass(frozen=True)
class TrajectoryProfile:
    """一次完整时间参数化的结果。"""

    polyline: Polyline
    lengths: np.ndarray
    node_speeds: np.ndarray
    corner_bounds: np.ndarray
    segments: tuple  # tuple[SegmentPlan, ...]
    total_time: float
    corner_info: tuple
    warnings: tuple

    @property
    def n_nodes(self) -> int:
        return self.polyline.n_nodes

    @property
    def start_speed(self) -> float:
        return float(self.node_speeds[0])

    @property
    def end_speed(self) -> float:
        return float(self.node_speeds[-1])


def _locate_segment(time: float, segments: tuple) -> tuple[SegmentPlan, float]:
    """按时间定位所在段，返回 (段计划, 段内时间)。超界钳到首/末段。"""
    if time <= 0.0:
        return segments[0], 0.0
    for plan in segments:
        if time <= plan.t_start + plan.duration + 1e-12:
            return plan, min(max(time - plan.t_start, 0.0), plan.duration)
    last = segments[-1]
    return last, last.duration


def _phase_state(plan: SegmentPlan, tau: float) -> tuple[float, float, float]:
    """段内局部状态，返回 (段内弧长 s, 速度 v, 切向加速度 a)。"""
    if plan.length == 0.0 or plan.duration == 0.0:
        return 0.0, plan.v_enter, 0.0

    if tau <= plan.t_acc:
        # 加速相
        v = plan.v_enter + (plan.v_peak - plan.v_enter) * (
            tau / plan.t_acc if plan.t_acc > 0.0 else 1.0
        )
        s = plan.v_enter * tau + 0.5 * (plan.v_peak - plan.v_enter) * (
            tau**2 / plan.t_acc if plan.t_acc > 0.0 else 0.0
        )
        return s, v, (plan.v_peak - plan.v_enter) / plan.t_acc if plan.t_acc > 0.0 else 0.0

    tau -= plan.t_acc
    if tau <= plan.t_coast:
        # 巡航相
        return plan.s_acc + plan.v_peak * tau, plan.v_peak, 0.0

    tau -= plan.t_coast
    if plan.t_dec > 0.0:
        frac = min(tau / plan.t_dec, 1.0)
        v = plan.v_peak - (plan.v_peak - plan.v_exit) * frac
        s = (
            plan.s_acc
            + plan.s_coast
            + plan.v_peak * tau
            - 0.5 * (plan.v_peak - plan.v_exit) * tau**2 / plan.t_dec
        )
        a = -(plan.v_peak - plan.v_exit) / plan.t_dec
        return s, v, a

    # 无制动相（v_peak == v_exit）
    return plan.s_acc + plan.s_coast, plan.v_exit, 0.0


def sample_at(profile: TrajectoryProfile, time: float) -> dict:
    """单个时刻采样：位置、速度、切向/合成加速度、航向。"""
    plan, tau = _locate_segment(float(time), profile.segments)
    s_local, speed, accel_tan = _phase_state(plan, tau)

    points = profile.polyline.points
    direction = points[plan.index + 1] - points[plan.index]
    unit = direction / plan.length if plan.length > 0.0 else np.zeros(points.shape[1])
    position = points[plan.index] + unit * s_local
    velocity = unit * speed
    acceleration = unit * accel_tan

    return {
        "t": float(min(max(float(time), 0.0), profile.total_time)),
        "position": np.asarray(position, dtype=float),
        "velocity": np.asarray(velocity, dtype=float),
        "speed": float(speed),
        "acceleration": np.asarray(acceleration, dtype=float),
        "tangential_accel": float(accel_tan),
        "segment": plan.index,
        "s_segment": float(s_local),
        "heading_xy": float(np.arctan2(unit[1], unit[0])) if unit.shape[0] >= 2 else 0.0,
    }


def sample_grid(profile: TrajectoryProfile, dt: float) -> dict:
    """按固定步长（秒）稠密采样，返回各量数组（合成数据，供离线校验）。"""
    if dt <= 0.0:
        raise ValueError("采样步长 dt 必须为正数")
    n_steps = int(np.floor(profile.total_time / dt)) + 1
    times = np.array([i * dt for i in range(n_steps)] + [profile.total_time])
    times = np.unique(np.clip(times, 0.0, profile.total_time))

    samples = [sample_at(profile, t) for t in times]
    dim = profile.polyline.points.shape[1]
    return {
        "t": times,
        "position": np.vstack([s["position"] for s in samples]).reshape(-1, dim),
        "velocity": np.vstack([s["velocity"] for s in samples]).reshape(-1, dim),
        "speed": np.array([s["speed"] for s in samples]),
        "acceleration": np.vstack([s["acceleration"] for s in samples]).reshape(-1, dim),
        "tangential_accel": np.array([s["tangential_accel"] for s in samples]),
        "segment": np.array([s["segment"] for s in samples]),
    }


def validate_profile(
    profile: TrajectoryProfile,
    v_max: float,
    a_max: float,
    d_max: float,
    v_start_req: float,
    v_end_req: float,
    dt: float = 1e-3,
    tol: float = _NUMERIC_EPS,
) -> dict:
    """对档案做独立数值校验（不复用规划公式），用中心差分估算加速度。

    Returns:
        校验报告 dict：各项最大违反量与是否通过。
    """
    grid = sample_grid(profile, dt)
    t, speed = grid["t"], grid["speed"]

    # 数值微分：中心差分，端点前/后向差分
    dv = np.zeros_like(speed)
    dv[1:-1] = (speed[2:] - speed[:-2]) / (t[2:] - t[:-2])
    dv[0] = (speed[1] - speed[0]) / (t[1] - t[0]) if t.size > 1 else 0.0
    dv[-1] = (speed[-1] - speed[-2]) / (t[-1] - t[-2]) if t.size > 1 else 0.0

    v_violation = float(max(0.0, speed.max() - v_max))
    accel = dv
    a_violation = float(max(0.0, accel.max() - a_max))
    d_violation = float(max(0.0, -accel.min() - d_max))
    start_err = abs(speed[0] - v_start_req)
    end_err = abs(speed[-1] - v_end_req)

    # 位置连续性：相邻采样点在节点切换处不应跳变（由构造保证，数值复核）
    positions = grid["position"]
    gaps = np.linalg.norm(np.diff(positions, axis=0), axis=1)
    max_gap = float(gaps.max(initial=0.0))

    checks = {
        "speed_within_limit": bool(v_violation <= tol * max(1.0, v_max)),
        "accel_within_limit": bool(a_violation <= tol * max(1.0, a_max) + 1e-3),
        "decel_within_limit": bool(d_violation <= tol * max(1.0, d_max) + 1e-3),
        "start_speed": bool(start_err <= max(tol, 1e-6)),
        "end_speed": bool(end_err <= max(tol, 1e-6)),
        "total_time_positive": bool(
            profile.total_time > 0.0 or profile.polyline.total_length == 0.0
        ),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "max_speed": float(speed.max()),
        "max_accel_numeric": float(accel.max()),
        "max_decel_numeric": float(-accel.min()),
        "speed_violation": v_violation,
        "accel_violation": a_violation,
        "decel_violation": d_violation,
        "start_speed_measured": float(speed[0]),
        "end_speed_measured": float(speed[-1]),
        "start_speed_error": float(start_err),
        "end_speed_error": float(end_err),
        "total_time": float(profile.total_time),
        "max_position_gap": max_gap,
        "n_samples": int(t.size),
    }
