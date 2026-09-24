"""可达性预检（球形腕的 2 连杆平面解析解）。

腕点（frame 4 原点）：
  ρ·cos(q1), ρ·sin(q1), z_wc
  ρ  = a2·cos q2 + a3·cos(q2+q3) + d4·sin(q2+q3)
  z' = z_wc - d1
     = a2·sin q2 + a3·sin(q2+q3) - d4·cos(q2+q3)

令 L3 = sqrt(a3²+d4²)、δ = atan2(d4, a3)，则
  ρ  = a2·cos q2 + L3·cos(q2+q3-δ)
  z' = a2·sin q2 + L3·sin(q2+q3-δ)
化为标准 2 连杆问题（相对肘角 D = q3 - δ）：
  cos D = (dist² - a2² - L3²) / (2·a2·L3)
  q2 = φ - ψ(D)，q3 = D + δ，φ = atan2(z', ρ)
肘上/肘下两支 D = ±arccos(cos D)。

注意：本机械臂真正的“伸直奇异”在 q3 = δ（a3、d4 合成方向与 a2 共线，
腕距达到 a2+L3），而不是直观的 q3 = 0。

据此精确区分：
  - 完全超出臂展           → UNREACHABLE（对球形腕是充分证明）
  - 几何可达但关节限位不允许 → LIMIT_CONFLICT 候选证据之一
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .angles import nearest_equivalent_in_range
from .robot_model import RobotModel


@dataclass
class WristFeasibility:
    reachable: bool
    """几何臂展内（忽略限位）。"""
    within_limits: bool
    """存在满足 q1/q2/q3 限位的腕部构型。"""
    max_reach: float
    """肩轴到腕点的最大可达距离（诊断用）。"""
    requested_radius: float
    """目标腕点到肩轴的距离（诊断用）。"""
    reason: str | None = None


def wrist_reachability(robot: RobotModel, wc: np.ndarray) -> WristFeasibility:
    a2 = float(robot.a[1])
    a3 = float(robot.a[2])
    d1 = float(robot.d[0])
    d4 = float(robot.d[3])
    l3 = float(np.hypot(a3, d4))
    delta = float(np.arctan2(d4, a3))

    x, y = float(wc[0]), float(wc[1])
    zp = float(wc[2]) - d1
    radius = float(np.hypot(x, y))
    dist = float(np.hypot(radius, zp))
    max_reach = a2 + l3

    d_cos = (dist**2 - a2**2 - l3**2) / (2.0 * a2 * l3)
    if d_cos > 1.0 + 1e-7:
        return WristFeasibility(False, False, max_reach, dist, "out_of_workspace")
    d_cos = float(np.clip(d_cos, -1.0, 1.0))

    # 腕点位于基座 z 轴上时方位角退化：此时 q1 可取任意值，用 0 与 π 代表两支，
    # 二者至少有一个落在 ±2.967 rad 的 q1 限位内，不会误判限位冲突。
    base_angle = float(np.arctan2(y, x)) if radius > 1e-9 else 0.0

    phi = float(np.arctan2(zp, radius)) if (radius > 1e-12 or abs(zp) > 1e-12) else 0.0
    d0 = float(np.arccos(d_cos))
    any_limits = False
    candidates = []
    for rel in (d0, -d0):
        psi = float(np.arctan2(l3 * np.sin(rel), a2 + l3 * np.cos(rel)))
        q3 = rel + delta
        # 直接支：肩平面 r=+ρ
        candidates.append((base_angle, phi - psi, q3))
        # 方位翻转支：r=-ρ，对侧肘支几何
        psi_op = float(np.arctan2(l3 * np.sin(-rel), a2 + l3 * np.cos(-rel)))
        candidates.append((base_angle + np.pi, psi_op - phi + np.pi, q3))
    for cq1, q2, q3 in candidates:
        q1c = nearest_equivalent_in_range(cq1, float(robot.joint_lower[0]), float(robot.joint_upper[0]))
        q2c = nearest_equivalent_in_range(q2, float(robot.joint_lower[1]), float(robot.joint_upper[1]))
        q3c = nearest_equivalent_in_range(q3, float(robot.joint_lower[2]), float(robot.joint_upper[2]))
        if q1c is not None and q2c is not None and q3c is not None:
            any_limits = True

    if not any_limits:
        return WristFeasibility(True, False, max_reach, dist, "joint_limit_conflict")
    return WristFeasibility(True, True, max_reach, dist, None)
