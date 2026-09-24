"""二维双连杆机械臂正/逆运动学核心。

约定:
- 关节角 q = (theta1, theta2), 单位弧度。
- theta1 为基座关节角(相对 x 轴), theta2 为肘关节相对角。
- 正解: x = L1*cos(t1) + L2*cos(t1+t2), y = L1*sin(t1) + L2*sin(t1+t2)。
- 逆解给出两支解析解: elbow_down (theta2 >= 0 分支) 与 elbow_up (theta2 <= 0 分支)。
- 不做任何坐标裁剪: 目标不可达或违反关节限位时如实返回状态, 绝不伪造成功。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class IKStatus(str, Enum):
    OK = "ok"                       # 至少一支解有效
    UNREACHABLE = "unreachable"     # 目标超出工作空间 (|r - L1| > d 或 d > L1 + L2)
    LIMIT_VIOLATION = "limit_violation"  # 几何可达, 但两支解都违反关节限位
    SINGULAR = "singular"           # 完全伸直/完全折叠, 两支解退化为同一解


@dataclass(frozen=True)
class JointLimits:
    """关节限位, 弧度, 闭区间 [lower, upper]。"""
    theta1_min: float = -math.pi
    theta1_max: float = math.pi
    theta2_min: float = -math.pi
    theta2_max: float = math.pi

    def contains(self, q: tuple[float, float], tol: float = 1e-9) -> bool:
        t1, t2 = q
        return (
            self.theta1_min - tol <= t1 <= self.theta1_max + tol
            and self.theta2_min - tol <= t2 <= self.theta2_max + tol
        )


@dataclass(frozen=True)
class ArmModel:
    l1: float = 1.0
    l2: float = 1.0
    limits: JointLimits = field(default_factory=JointLimits)

    def __post_init__(self) -> None:
        if self.l1 <= 0 or self.l2 <= 0:
            raise ValueError("link lengths must be positive")


@dataclass(frozen=True)
class IKSolution:
    branch: str               # "elbow_down" | "elbow_up"
    theta1: float
    theta2: float
    within_limits: bool


@dataclass(frozen=True)
class IKResult:
    status: IKStatus
    solutions: tuple[IKSolution, ...]   # 可能为 0/1/2 支
    reach_distance: float               # 目标到基座距离 d
    singular: bool                      # 是否处于奇异(伸直/折叠)位形附近


# 判定奇异的余弦容差: |cos(theta2)| > 1 - SINGULAR_COS_TOL 视为伸直/折叠
SINGULAR_COS_TOL = 1e-9
# 几何可达性容差, 吸收浮点误差
REACH_TOL = 1e-9


def wrap_to_pi(angle: float) -> float:
    """把角度规范化到 (-pi, pi]。"""
    a = angle % (2.0 * math.pi)  # [0, 2pi)
    if a > math.pi:
        a -= 2.0 * math.pi
    return a


def forward_kinematics(model: ArmModel, theta1: float, theta2: float) -> tuple[float, float]:
    """正解: 关节角 -> 末端位置 (x, y)。"""
    x = model.l1 * math.cos(theta1) + model.l2 * math.cos(theta1 + theta2)
    y = model.l1 * math.sin(theta1) + model.l2 * math.sin(theta1 + theta2)
    return x, y


def _law_of_cosines_theta2(model: ArmModel, d2: float) -> float:
    """由余弦定理求 cos(theta2), 并做数值钳制到 [-1, 1]。

    钳制只用于吸收浮点噪声(量级 1e-12); 真正的不可达在调用前已用
    REACH_TOL 判定并返回 UNREACHABLE, 不会走到这里被"钳"成可达。
    """
    c2 = (d2 - model.l1**2 - model.l2**2) / (2.0 * model.l1 * model.l2)
    return max(-1.0, min(1.0, c2))


def inverse_kinematics(
    model: ArmModel,
    x: float,
    y: float,
    limits: Optional[JointLimits] = None,
) -> IKResult:
    """解析逆解: 返回两支解(elbow_down / elbow_up)及状态。

    - 不可达: 不裁剪坐标, 直接返回 UNREACHABLE, solutions 为空。
    - 奇异(完全伸直 theta2=0 或完全折叠 |theta2|=pi): 两支退化为同一解,
      返回 SINGULAR 与唯一解。
    - 几何可达但两支都违反限位: LIMIT_VIOLATION, 仍附上两支解供诊断。
    """
    lim = limits if limits is not None else model.limits
    d2 = x * x + y * y
    d = math.sqrt(d2)
    r_max = model.l1 + model.l2
    r_min = abs(model.l1 - model.l2)

    if d > r_max + REACH_TOL or d < r_min - REACH_TOL:
        return IKResult(
            status=IKStatus.UNREACHABLE,
            solutions=(),
            reach_distance=d,
            singular=False,
        )

    c2 = _law_of_cosines_theta2(model, d2)
    singular = abs(c2) > 1.0 - SINGULAR_COS_TOL

    # 基座方向角; 原点附近(d≈0, 仅当 L1==L2 时可达)方向任意, 取 0
    phi = math.atan2(y, x) if d > 0.0 else 0.0
    # 大臂与基座-目标连线的夹角
    cos_alpha = (d2 + model.l1**2 - model.l2**2) / (2.0 * model.l1 * d) if d > 0.0 else 1.0
    cos_alpha = max(-1.0, min(1.0, cos_alpha))
    alpha = math.acos(cos_alpha)

    s2 = math.sqrt(max(0.0, 1.0 - c2 * c2))

    branches: list[tuple[str, float]] = [("elbow_down", +s2)]
    if not singular:
        branches.append(("elbow_up", -s2))

    solutions: list[IKSolution] = []
    for name, sin_t2 in branches:
        t2 = math.atan2(sin_t2, c2)
        # elbow_down 取 theta2 >= 0 分支: alpha 取正; elbow_up 取负
        t1 = phi - alpha if sin_t2 >= 0.0 else phi + alpha
        t1 = wrap_to_pi(t1)
        q = (t1, t2)
        solutions.append(
            IKSolution(branch=name, theta1=t1, theta2=t2, within_limits=lim.contains(q))
        )

    if singular:
        status = IKStatus.SINGULAR
    elif any(s.within_limits for s in solutions):
        status = IKStatus.OK
    else:
        status = IKStatus.LIMIT_VIOLATION

    return IKResult(
        status=status,
        solutions=tuple(solutions),
        reach_distance=d,
        singular=singular,
    )


def angular_distance(a: float, b: float) -> float:
    """两个角度在环面上的最短距离, [0, pi]。"""
    return abs(wrap_to_pi(a - b))


def joint_distance(q1: tuple[float, float], q2: tuple[float, float]) -> float:
    """关节空间加权距离(此处等权), 用于连续选解。"""
    return math.hypot(
        angular_distance(q1[0], q2[0]), angular_distance(q1[1], q2[1])
    )


def solve_path(
    model: ArmModel,
    points: list[tuple[float, float]],
    seed: Optional[tuple[float, float]] = None,
    limits: Optional[JointLimits] = None,
) -> list[dict]:
    """沿路径逐点求逆解并做连续选解(贪心: 选与上一步关节角最近的合法支)。

    - 第一步选离 seed 最近的合法支; 无 seed 时选第一支合法解(elbow_down 优先)。
    - 某点无可行解时, 该点记录 status 与空关节角, 不中断后续点,
      也不把上一步关节角偷偷沿用(连续性链在该点断开, 之后重新起链)。
    返回每点的 dict: {target, status, branch, theta1, theta2, step_jump}。
    step_jump 为与上一有效解的关节距离, 首点或断链后为 None。
    """
    results: list[dict] = []
    prev_q: Optional[tuple[float, float]] = seed

    for x, y in points:
        res = inverse_kinematics(model, x, y, limits)
        feasible = [s for s in res.solutions if s.within_limits]

        if not feasible:
            results.append(
                {
                    "target": (x, y),
                    "status": res.status.value,
                    "branch": None,
                    "theta1": None,
                    "theta2": None,
                    "step_jump": None,
                }
            )
            prev_q = None  # 断链: 下一点重新选支
            continue

        if prev_q is None:
            chosen = feasible[0]  # 无先验: elbow_down 优先(列表顺序保证)
        else:
            chosen = min(
                feasible,
                key=lambda s: joint_distance((s.theta1, s.theta2), prev_q),
            )

        q = (chosen.theta1, chosen.theta2)
        jump = joint_distance(q, prev_q) if prev_q is not None else None
        results.append(
            {
                "target": (x, y),
                "status": res.status.value,
                "branch": chosen.branch,
                "theta1": chosen.theta1,
                "theta2": chosen.theta2,
                "step_jump": jump,
            }
        )
        prev_q = q

    return results
