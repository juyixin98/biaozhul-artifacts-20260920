"""连续碰撞检测核心：两个沿线性轨迹运动的圆盘的最早接触时间。

数学模型
--------
机器人 R、障碍物 O 均为二维圆盘，圆心随时间线性运动：

    p_R(t) = c_R + v_R * t
    p_O(t) = c_O + v_O * t

相对位置 d(t) = (c_R - c_O) + (v_R - v_O) * t。两圆接触（含相切）当且仅当

    |d(t)|^2 <= (r_R + r_O)^2

展开为关于 t 的二次方程 a*t^2 + b*t + c = 0，其中

    a = |v_rel|^2,  b = 2 * d0 · v_rel,  c = |d0|^2 - R^2

接触时间区间即该抛物线 <= 0 的解集，由解析求根得到——全程不做
离散时间采样，因此任意高速穿越（tunneling）都不会漏检。

区间闭合规则（明确约定）
------------------------
1. 接触区间 [t_enter, t_exit] 为**闭区间**：端点（恰好相切的瞬间）
   视为碰撞。
2. 相切（判别式为 0，二重根）视为碰撞，t_enter == t_exit。
3. 初始重叠（t=0 时已满足 |d0| < R）视为碰撞，t_enter 取窗口起点。
4. 时间窗口 [t_start, t_end] 两端均为**闭**：接触时刻恰好等于
   t_start 或 t_end 时视为窗口内碰撞。
5. 相对速度为零（a = 0）时间距恒定：当前重叠则整窗碰撞，否则不碰撞。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

from .models import CircleBody
from .quadratic import solve_quadratic

# 接触分类
KIND_NONE = "none"  # 窗口内无接触
KIND_OVERLAP = "overlap"  # 初始即重叠
KIND_CROSSING = "crossing"  # 进入并穿出（两个不同实根）
KIND_TANGENT = "tangent"  # 相切（二重根）

# 判定“相对速度为零”的容限（|v_rel|^2 的绝对阈值）
_ZERO_A_TOL = 1e-30


@dataclass(frozen=True)
class ContactInterval:
    """一对圆盘在时间窗口内的接触分析结果（不可变）。"""

    collides: bool
    kind: str
    t_enter: Optional[float] = None  # 窗口内最早接触时刻（闭区间端点）
    t_exit: Optional[float] = None  # 窗口内最晚接触时刻
    window: tuple = (0.0, 1.0)
    sum_radii: float = 0.0
    initial_distance: float = 0.0
    relative_speed: float = 0.0
    coefficients: tuple = ()  # (a, b, c)，便于审计与手算核对
    roots: tuple = ()  # 全时间轴上的实根（未裁剪到窗口）

    def as_dict(self) -> dict:
        return {
            "collides": self.collides,
            "kind": self.kind,
            "t_enter": self.t_enter,
            "t_exit": self.t_exit,
            "window": list(self.window),
            "sum_radii": self.sum_radii,
            "initial_distance": self.initial_distance,
            "relative_speed": self.relative_speed,
            "coefficients": list(self.coefficients),
            "roots": list(self.roots),
        }


def _validate_window(t_start: float, t_end: float) -> tuple:
    t0, t1 = float(t_start), float(t_end)
    if not (np.isfinite(t0) and np.isfinite(t1)):
        raise ValueError("时间窗口端点必须为有限实数")
    if t1 < t0:
        raise ValueError("时间窗口终点不得早于起点")
    return t0, t1


def analyze_pair(
    robot: CircleBody,
    obstacle: CircleBody,
    t_start: float = 0.0,
    t_end: float = 1.0,
) -> ContactInterval:
    """分析一对圆盘在闭窗口 [t_start, t_end] 内的最早接触时间。"""
    t0, t1 = _validate_window(t_start, t_end)

    d0 = robot.center - obstacle.center
    v_rel = robot.velocity - obstacle.velocity
    radius_sum = robot.radius + obstacle.radius

    a = float(v_rel @ v_rel)
    b = float(2.0 * (d0 @ v_rel))
    c = float(d0 @ d0) - radius_sum * radius_sum

    base = dict(
        window=(t0, t1),
        sum_radii=radius_sum,
        initial_distance=float(np.hypot(d0[0], d0[1])),
        relative_speed=float(np.hypot(v_rel[0], v_rel[1])),
        coefficients=(a, b, c),
    )

    # 规则 5：相对速度为零，间距恒定
    if a <= _ZERO_A_TOL:
        if c <= 0.0:  # 当前已接触/重叠，且永不分离
            return ContactInterval(
                collides=True,
                kind=KIND_OVERLAP,
                t_enter=t0,
                t_exit=t1,
                roots=(),
                **base,
            )
        return ContactInterval(collides=False, kind=KIND_NONE, roots=(), **base)

    roots = solve_quadratic(a, b, c)
    base["roots"] = roots

    if not roots:
        # 抛物线恒正：全程分离
        return ContactInterval(collides=False, kind=KIND_NONE, **base)

    if len(roots) == 1:
        # 规则 2：相切，接触区间退化为一个点（仍属闭区间碰撞）
        t_touch = roots[0]
        if t0 <= t_touch <= t1:
            return ContactInterval(
                collides=True,
                kind=KIND_TANGENT,
                t_enter=t_touch,
                t_exit=t_touch,
                **base,
            )
        return ContactInterval(collides=False, kind=KIND_NONE, **base)

    t_in, t_out = roots  # 全时间轴上的接触闭区间 [t_in, t_out]

    # 与窗口 [t0, t1] 求交（闭区间交闭区间）
    enter = max(t_in, t0)
    exit_ = min(t_out, t1)
    if enter > exit_:
        return ContactInterval(collides=False, kind=KIND_NONE, **base)

    # 规则 3：t=0（窗口起点）已处于接触区间内
    if t_in <= t0:
        kind = KIND_OVERLAP
    elif t_in == t_out:
        kind = KIND_TANGENT
    else:
        kind = KIND_CROSSING

    return ContactInterval(
        collides=True, kind=kind, t_enter=enter, t_exit=exit_, **base
    )


def analyze_request(
    robot: CircleBody,
    obstacles: Sequence[CircleBody],
    t_start: float = 0.0,
    t_end: float = 1.0,
) -> list:
    """对机器人与每个障碍分别做连续碰撞分析，保持输入顺序。"""
    return [analyze_pair(robot, ob, t_start, t_end) for ob in obstacles]


def earliest_collision(results: Sequence[ContactInterval]) -> Optional[ContactInterval]:
    """从若干结果中取窗口内最早接触者；全部安全时返回 None。"""
    hits = [r for r in results if r.collides]
    if not hits:
        return None
    return min(hits, key=lambda r: (r.t_enter, r.kind))
