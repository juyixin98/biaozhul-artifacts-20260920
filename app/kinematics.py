"""二维双连杆机械臂运动学核心：正解、解析逆解、关节限位、沿路径连续选解。

坐标约定
--------
- 平面坐标，基座（肩关节）固定在原点 (0, 0)。
- q1（theta1）：第一连杆相对 +x 轴的角度（弧度）。
- q2（theta2）：第二连杆相对第一连杆的相对角度（弧度）。
- 末端位置::

      x = l1 cos(q1) + l2 cos(q1 + q2)
      y = l1 sin(q1) + l2 sin(q1 + q2)

肘部命名约定（几何约定，避免不同教材正负号差异）
--------------------------------------------------
用前臂向量与大臂向量的二维叉积判定肘部位于“肩→末端”连线的哪一侧：

    cross = (W - O) x (E - O),  O=肩 W=肘 E=末端

cross < 0 记为 ``elbow_up``（对应解析解 q2 = -alpha），
cross > 0 记为 ``elbow_down``（q2 = +alpha）。
在完全伸直 / 完全折叠（奇异）附近 cross → 0，分支退化。

设计原则：**绝不通过裁剪目标坐标把不可达点伪装成成功**。
不可达时 ``status`` 明确给出 ``UNREACHABLE``，并返回到工作区的
径向距离量；限位冲突时给出 ``LIMIT_VIOLATION`` 与各分支超限情况。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional

import numpy as np

TWO_PI = 2.0 * math.pi

# 判定严格奇异配置（q2≈0 伸直 / q2≈±pi 折叠）的 |sin(q2)| 机器精度阈值。
# 与工作区边界容差解耦：边界容差用于可达性判定与 acos 裁剪，
# 奇异判定基于实际关节构型，保证 NEAR_SINGULAR 区间不被容差带吞掉。
EXACT_SINGULAR_EPS = 1e-11


class IKStatus(str, Enum):
    """逆解 / 路径点的求解状态。"""

    OK = "ok"
    """正常可达、非奇异、满足关节限位。"""

    NEAR_SINGULAR = "near_singular"
    """可达且满足限位，但 |sin(q2)| 很小（接近完全伸直或折叠），
    两支解析解重合，雅可比病态。"""

    FULL_EXTENSION = "full_extension"
    """末端位于工作区外边界 r = l1 + l2（完全伸直，q2 = 0），奇异。"""

    FULL_FOLD = "full_fold"
    """等长臂末端位于原点（完全折叠，q2 = ±pi），奇异。"""

    UNREACHABLE = "unreachable"
    """目标在工作区之外（r > l1 + l2，或等长臂时 r < 内部空洞）。
    返回 None，不做坐标裁剪。"""

    LIMIT_VIOLATION = "limit_violation"
    """目标几何上可达，但两支解析解都不满足关节限位。
    返回 None，并给出各分支的超限明细。"""


@dataclass(frozen=True)
class LinkParams:
    """连杆参数与关节限位。

    角度限位单位为弧度；取值区间可以跨越 ±pi（例如 (-3pi/2, pi/2)），
    内部会以 2*pi*k 的等价表示检查可达性。
    """

    l1: float = 1.0
    l2: float = 1.0
    theta1_min: float = -math.pi
    theta1_max: float = math.pi
    theta2_min: float = -math.pi
    theta2_max: float = math.pi
    # 几何容差（相对臂展比例）：判定“恰好”工作区边界
    reach_tol_ratio: float = 1e-9
    # 判定近奇异的 |sin(q2)| 阈值
    singular_eps: float = 1e-6

    def __post_init__(self) -> None:
        if self.l1 <= 0.0 or self.l2 <= 0.0:
            raise ValueError("连杆长度 l1、l2 必须为正数")
        if self.theta1_min > self.theta1_max:
            raise ValueError("theta1 限位要求 min <= max")
        if self.theta2_min > self.theta2_max:
            raise ValueError("theta2 限位要求 min <= max")
        if not 0.0 < self.reach_tol_ratio < 1e-3:
            raise ValueError("reach_tol_ratio 需落在 (0, 1e-3)")
        if not 0.0 < self.singular_eps < 1.0:
            raise ValueError("singular_eps 需落在 (0, 1)")

    @property
    def reach(self) -> float:
        """最大可达半径 l1 + l2。"""
        return self.l1 + self.l2

    @property
    def inner_radius(self) -> float:
        """内边界 |l1 - l2|（等长臂时为 0）。"""
        return abs(self.l1 - self.l2)

    @property
    def reach_tol(self) -> float:
        """工作区边界的绝对容差。"""
        return self.reach_tol_ratio * self.reach


def forward_kinematics(q1: float, q2: float, params: LinkParams) -> np.ndarray:
    """正运动学：关节角 -> 末端 (x, y)，形状 (2,) 的 ndarray。"""
    return np.array(
        [
            params.l1 * math.cos(q1) + params.l2 * math.cos(q1 + q2),
            params.l1 * math.sin(q1) + params.l2 * math.sin(q1 + q2),
        ],
        dtype=float,
    )


def elbow_position(q1: float, q2: float, params: LinkParams) -> np.ndarray:
    """肘部位置（l1 末端），形状 (2,)。"""
    return np.array(
        [params.l1 * math.cos(q1), params.l1 * math.sin(q1)], dtype=float
    )


def jacobian(q1: float, q2: float, params: LinkParams) -> np.ndarray:
    """解析几何雅可比，形状 (2, 2)，列分别为 d(末端)/dq1、d(末端)/dq2。

    det(J) = l1 * l2 * sin(q2)，故完全伸直 / 折叠时雅可比奇异。
    """
    s1, c1 = math.sin(q1), math.cos(q1)
    s12, c12 = math.sin(q1 + q2), math.cos(q1 + q2)
    return np.array(
        [
            [-params.l1 * s1 - params.l2 * s12, -params.l2 * s12],
            [params.l1 * c1 + params.l2 * c12, params.l2 * c12],
        ],
        dtype=np.float64,
    )


def wrap_to_pi(angle: float) -> float:
    """把角度包裹到 [-pi, pi)。"""
    return (angle + math.pi) % TWO_PI - math.pi


def representative(angle: float, lo: float, hi: float) -> Optional[float]:
    """返回 angle 落在闭区间 [lo, hi] 内的 2*pi 等价表示；不存在则 None。"""
    k0 = math.floor((lo - angle) / TWO_PI + 0.5)
    for k in range(k0 - 1, k0 + 2):
        cand = angle + k * TWO_PI
        if lo <= cand <= hi:
            return cand
    return None


@dataclass(frozen=True)
class Branch:
    """单支解析逆解。"""

    name: str
    """``elbow_up`` / ``elbow_down`` / ``degenerate``（奇异退化支）。"""

    q1: float
    """包裹到 [-pi, pi) 的第一关节角。"""

    q2: float
    """包裹到 [-pi, pi) 的第二关节角。"""

    feasible: bool
    """是否存在满足关节限位的 2*pi 等价表示。"""

    q1_limited: Optional[float]
    """满足限位的 q1 表示（不存在为 None）。"""

    q2_limited: Optional[float]
    """满足限位的 q2 表示（不存在为 None）。"""

    violations: tuple[str, ...]
    """超限关节名（``theta1`` / ``theta2``），feasible 时为空。"""

    ee: np.ndarray
    """该解析解重新正解得到的末端位置（理论上应贴近目标）。"""

    error: float
    """该解析解的末端误差（正解 vs 目标）。"""

    cross: float
    """(W-O) x (E-O)，用于肘部命名与退化判定。"""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "q1": self.q1,
            "q2": self.q2,
            "feasible": self.feasible,
            "q1_limited": self.q1_limited,
            "q2_limited": self.q2_limited,
            "violations": list(self.violations),
            "ee": [float(self.ee[0]), float(self.ee[1])],
            "error": float(self.error),
            "cross": float(self.cross),
        }


@dataclass(frozen=True)
class IKSolution:
    """单个目标点的逆解结果。

    不可达 / 限位冲突时 ``joints`` 为 None；调用方必须检查 ``status``，
    服务层不会返回“看起来成功”的裁剪坐标。
    """

    reachable: bool
    status: IKStatus
    branches: tuple[Branch, ...]
    target: np.ndarray
    joints: Optional[tuple[float, float]]
    """选中解（满足限位的 2*pi 等价表示），不可用为 None。"""

    chosen: Optional[str]
    """选中分支名。"""

    flipped: bool
    """相比上一关节参考是否发生肘部翻转。"""

    end_effector: Optional[np.ndarray]
    """选中解正解回算的末端位置。"""

    position_error: Optional[float]
    """选中解末端到目标的欧氏误差。"""

    boundary_gap: Optional[float]
    """不可达时到最近工作区边界的径向距离。"""

    singular: bool
    """是否处于奇异（完全伸直 / 折叠）。"""

    near_singular: bool
    """是否近奇异。"""

    note: str

    def as_dict(self) -> dict:
        return {
            "reachable": self.reachable,
            "status": self.status.value,
            "branches": [b.as_dict() for b in self.branches],
            "target": [float(self.target[0]), float(self.target[1])],
            "joints": None
            if self.joints is None
            else [float(self.joints[0]), float(self.joints[1])],
            "chosen": self.chosen,
            "flipped": self.flipped,
            "end_effector": None
            if self.end_effector is None
            else [float(self.end_effector[0]), float(self.end_effector[1])],
            "position_error": self.position_error,
            "boundary_gap": self.boundary_gap,
            "singular": self.singular,
            "near_singular": self.near_singular,
            "note": self.note,
        }


def _raw_branches(target: np.ndarray, p: LinkParams) -> tuple[list[Branch], bool, bool, str]:
    """计算两支原始解析解。

    返回 (branches, singular, near_singular, note)。
    不可达由调用方根据返回的空列表判定。
    """
    x, y = float(target[0]), float(target[1])
    r = math.hypot(x, y)
    l1, l2 = p.l1, p.l2
    tol = p.reach_tol

    outer = l1 + l2
    inner = abs(l1 - l2)

    # --- 工作区判定（不裁剪坐标） ---
    if r > outer + tol:
        return [], False, False, f"目标半径 {r:.6g} 超出最大可达半径 {outer:.6g}"
    if r < inner - tol:
        return [], False, False, (
            f"目标半径 {r:.6g} 小于内边界 {inner:.6g}（l1!=l2 时的工作区空洞）"
        )

    # 数值保护：只在容差带内夹到余弦边界
    c2 = (r * r - l1 * l1 - l2 * l2) / (2.0 * l1 * l2)
    at_outer = r >= outer - tol
    at_inner = r <= inner + tol
    if at_outer and c2 > 1.0:
        c2 = 1.0
    if at_inner and c2 < -1.0:
        c2 = -1.0
    c2 = max(-1.0, min(1.0, c2))
    alpha = math.acos(c2)
    s2 = abs(math.sin(alpha))

    # 严格奇异：构型层面 q2≈0（伸直）或 q2≈±pi（折叠）
    extended = s2 < EXACT_SINGULAR_EPS and c2 > 0.0
    folded = s2 < EXACT_SINGULAR_EPS and c2 < 0.0
    singular = extended or folded or r <= tol
    near_singular = (not singular) and s2 < p.singular_eps

    # q1 = phi ± beta，cos(beta) = (r^2 + l1^2 - l2^2)/(2 r l1)
    if r <= tol:
        # 原点：仅等长臂完全折叠可达（l1==l2 时 inner==0 已放行）。
        # q2=±pi 包裹后同为 -pi；q1 不可辨识，取规范值 0。
        raws: list[tuple[float, float, str]] = [(math.pi, 0.0, "fold")]
        note = "末端在原点：等长臂完全折叠奇异，q1 不可辨识，取规范值 0"
    else:
        phi = math.atan2(y, x)
        cb = (r * r + l1 * l1 - l2 * l2) / (2.0 * r * l1)
        cb = max(-1.0, min(1.0, cb))
        beta = math.acos(cb)
        if extended:
            # 完全伸直：alpha≈beta≈0，两支重合，只生成一支
            raws = [(0.0, phi, "ext")]
            note = "完全伸直奇异：两支解析解重合（q2=0）"
        elif folded:
            # 完全折叠：alpha≈pi，q1=phi±beta 两支（l1!=l2 时位于内边界）
            raws = [
                (alpha, wrap_to_pi(phi + beta), "fold_a"),
                (-alpha, wrap_to_pi(phi - beta), "fold_b"),
            ]
            note = "完全折叠奇异：两支解析解重合（q2=±pi）"
        else:
            # 肘上 q2=-alpha -> q1=phi+beta；肘下 q2=+alpha -> q1=phi-beta
            raws = [
                (-alpha, phi + beta, "up"),
                (alpha, phi - beta, "down"),
            ]
            note = ""

    branches: list[Branch] = []
    for raw_q2, raw_q1, tag in raws:
        if r <= tol:
            raw_q1 = 0.0
        q1w = wrap_to_pi(raw_q1)
        q2w = wrap_to_pi(raw_q2)

        q1lim = representative(q1w, p.theta1_min, p.theta1_max)
        q2lim = representative(q2w, p.theta2_min, p.theta2_max)
        violations: list[str] = []
        if q1lim is None:
            violations.append("theta1")
        if q2lim is None:
            violations.append("theta2")

        ee = forward_kinematics(q1w, q2w, p)
        err = float(np.linalg.norm(ee - target))
        elbow = elbow_position(q1w, q2w, p)
        cross = float(elbow[0] * ee[1] - elbow[1] * ee[0])

        if singular:
            name = "degenerate"
        elif tag == "up" or cross < 0.0:
            name = "elbow_up"
        else:
            name = "elbow_down"

        branches.append(
            Branch(
                name=name,
                q1=q1w,
                q2=q2w,
                feasible=not violations,
                q1_limited=q1lim,
                q2_limited=q2lim,
                violations=tuple(violations),
                ee=ee,
                error=err,
                cross=cross,
            )
        )

    # 折叠点两支可能数值上完全相同（等长臂），去重
    if folded and len(branches) == 2:
        b0, b1 = branches
        if abs(b0.q1 - b1.q1) < 1e-10 and abs(b0.q2 - b1.q2) < 1e-10:
            branches = [b0]

    return branches, singular, near_singular, note


def inverse_kinematics(
    target: Iterable[float],
    params: LinkParams,
    previous_joints: Optional[tuple[float, float]] = None,
    previous_branch: Optional[str] = None,
    preference: Optional[str] = None,
    hysteresis: float = 1e-6,
    weights: tuple[float, float] = (1.0, 1.0),
) -> IKSolution:
    """二维双连杆解析逆解。

    参数
    ----
    target:
        目标末端位置 (x, y)。
    params:
        连杆参数与关节限位。
    previous_joints:
        上一可行点的关节角（2*pi 等价表示），用于沿路径连续选解。
    previous_branch:
        上一可行点的分支名（``elbow_up`` / ``elbow_down``）。
        翻转判定与滞回以分支名为准——仅凭关节角符号推断在跨越
        ±pi 或奇异点后不可靠，因此分支名由路径求解器显式记忆传递。
        为 None 时不报告翻转。
    preference:
        ``"elbow_up"`` / ``"elbow_down"``，显式指定肘部分支；
        指定后该分支若撞限位直接返回 ``LIMIT_VIOLATION``，不静默换支。
    hysteresis:
        连续性代价的滞回系数：与上一解同名的分支额外减去 hysteresis，
        用于两支代价接近时抑制抖动；设为 0 退化为纯最短角距离。
    weights:
        (dq1, dq2) 的连续性权重。

    失败时 ``joints=None``，状态明确为 UNREACHABLE / LIMIT_VIOLATION；
    奇异可达时仍返回解，但 status 标记 FULL_EXTENSION / FULL_FOLD /
    NEAR_SINGULAR。严格奇异时仅 ``singular=True``；近奇异（非严格）
    时仅 ``near_singular=True``。
    """
    t = np.asarray(target, dtype=float)
    if t.shape != (2,):
        raise ValueError("target 必须是长度为 2 的 (x, y)")
    if preference not in (None, "elbow_up", "elbow_down"):
        raise ValueError("preference 只能是 None / 'elbow_up' / 'elbow_down'")
    if previous_branch not in (
        None,
        "elbow_up",
        "elbow_down",
        "degenerate",
    ):
        raise ValueError("previous_branch 取值非法")

    r = float(np.hypot(t[0], t[1]))
    branches, singular, near_sing_raw, raw_note = _raw_branches(t, params)
    # 严格奇异点不再同时标记近奇异
    near_sing = near_sing_raw and not singular

    if not branches:
        # 不可达：计算到最近工作区边界的径向距离，不裁剪坐标
        if r > params.reach:
            gap = r - params.reach
        else:
            gap = params.inner_radius - r
        return IKSolution(
            reachable=False,
            status=IKStatus.UNREACHABLE,
            branches=(),
            target=t,
            joints=None,
            chosen=None,
            flipped=False,
            end_effector=None,
            position_error=None,
            boundary_gap=float(max(gap, 0.0)),
            singular=False,
            near_singular=False,
            note=raw_note,
        )

    feasible = [b for b in branches if b.feasible]

    # --- 状态分类（奇异信息优先保留，即使限位冲突） ---
    if singular:
        # 原点折叠或 c2<0 的构型为折叠；否则为伸直
        folded_cfg = r <= params.reach_tol
        if not folded_cfg:
            c2_cfg = (r * r - params.l1**2 - params.l2**2) / (
                2.0 * params.l1 * params.l2
            )
            folded_cfg = c2_cfg < 0.0
        status = IKStatus.FULL_FOLD if folded_cfg else IKStatus.FULL_EXTENSION
    elif near_sing:
        status = IKStatus.NEAR_SINGULAR
    else:
        status = IKStatus.OK

    if not feasible:
        detail = "; ".join(
            f"{b.name} 违反 {','.join(b.violations)}" for b in branches
        )
        return IKSolution(
            reachable=True,
            status=IKStatus.LIMIT_VIOLATION,
            branches=tuple(branches),
            target=t,
            joints=None,
            chosen=None,
            flipped=False,
            end_effector=None,
            position_error=None,
            boundary_gap=None,
            singular=singular,
            near_singular=near_sing,
            note=f"两支解析解均不满足关节限位：{detail}",
        )

    # --- 选支 ---
    def cost(b: Branch) -> float:
        d1 = abs(wrap_to_pi(b.q1_limited - (previous_joints[0] if previous_joints else 0.0)))
        d2 = abs(wrap_to_pi(b.q2_limited - (previous_joints[1] if previous_joints else 0.0)))
        c = weights[0] * d1 + weights[1] * d2
        if previous_branch is not None and hysteresis and b.name != "degenerate":
            if b.name == previous_branch:
                c -= hysteresis
        return c

    if preference is not None:
        chosen = next((b for b in feasible if b.name == preference), None)
        if chosen is None:
            return IKSolution(
                reachable=True,
                status=IKStatus.LIMIT_VIOLATION,
                branches=tuple(branches),
                target=t,
                joints=None,
                chosen=None,
                flipped=False,
                end_effector=None,
                position_error=None,
                boundary_gap=None,
                singular=singular,
                near_singular=near_sing,
                note=f"显式偏好 {preference} 的分支不满足关节限位",
            )
    else:
        chosen = min(feasible, key=cost)

    joints = (chosen.q1_limited, chosen.q2_limited)
    ee = forward_kinematics(joints[0], joints[1], params)
    err = float(np.linalg.norm(ee - t))

    flipped = False
    if (
        previous_branch is not None
        and chosen.name != "degenerate"
        and previous_branch != "degenerate"
    ):
        flipped = chosen.name != previous_branch

    note = raw_note
    if preference is None and previous_joints is None:
        note = (note or "无历史参考，按默认最小角距离选支").strip() or "默认选支"

    return IKSolution(
        reachable=True,
        status=status,
        branches=tuple(branches),
        target=t,
        joints=joints,
        chosen=chosen.name,
        flipped=flipped,
        end_effector=ee,
        position_error=err,
        boundary_gap=None,
        singular=singular,
        near_singular=near_sing,
        note=note,
    )


@dataclass(frozen=True)
class PathPointResult:
    """路径上单个目标点的结果。"""

    index: int
    target: tuple[float, float]
    reachable: bool
    status: IKStatus
    joints: Optional[tuple[float, float]]
    chosen: Optional[str]
    flipped: bool
    position_error: Optional[float]
    boundary_gap: Optional[float]
    singular: bool
    near_singular: bool
    note: str

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "target": [float(self.target[0]), float(self.target[1])],
            "reachable": self.reachable,
            "status": self.status.value,
            "joints": None
            if self.joints is None
            else [float(self.joints[0]), float(self.joints[1])],
            "chosen": self.chosen,
            "flipped": self.flipped,
            "position_error": self.position_error,
            "boundary_gap": self.boundary_gap,
            "singular": self.singular,
            "near_singular": self.near_singular,
            "note": self.note,
        }


@dataclass
class PathResult:
    """整条路径的连续逆解结果与汇总指标。"""

    points: list[PathPointResult] = field(default_factory=list)
    solved_count: int = 0
    failed_count: int = 0
    flip_count: int = 0
    singular_count: int = 0
    near_singular_count: int = 0
    max_position_error: Optional[float] = None
    mean_position_error: Optional[float] = None
    max_joint_step: Optional[float] = None
    """相邻可行点间的最大关节跳变（包裹角距离，加权前）。"""

    def as_dict(self) -> dict:
        return {
            "points": [p.as_dict() for p in self.points],
            "solved_count": self.solved_count,
            "failed_count": self.failed_count,
            "flip_count": self.flip_count,
            "singular_count": self.singular_count,
            "near_singular_count": self.near_singular_count,
            "max_position_error": self.max_position_error,
            "mean_position_error": self.mean_position_error,
            "max_joint_step": self.max_joint_step,
        }


class ContinuousIKSolver:
    """沿目标序列连续选解的有状态求解器。

    - 每个目标在所有满足限位的解析分支中，选择相对上一可行点
      加权关节角距离最小的一支（角度按 2*pi 包裹处理）。
    - 不可达 / 限位冲突点 joints 为 None，且**不更新**参考关节角；
      下一个可行点仍以最近一个可行点为连续性基准。
    """

    def __init__(
        self,
        params: LinkParams,
        hysteresis: float = 1e-6,
        weights: tuple[float, float] = (1.0, 1.0),
    ) -> None:
        self.params = params
        self.hysteresis = hysteresis
        self.weights = weights
        self._previous: Optional[tuple[float, float]] = None
        self._previous_branch: Optional[str] = None

    def reset(self) -> None:
        self._previous = None
        self._previous_branch = None

    @property
    def previous_joints(self) -> Optional[tuple[float, float]]:
        return self._previous

    @property
    def previous_branch(self) -> Optional[str]:
        return self._previous_branch

    def solve(self, target: Iterable[float]) -> IKSolution:
        sol = inverse_kinematics(
            target,
            self.params,
            previous_joints=self._previous,
            previous_branch=self._previous_branch,
            hysteresis=self.hysteresis,
            weights=self.weights,
        )
        if sol.joints is not None:
            self._previous = sol.joints
            self._previous_branch = sol.chosen
        return sol

    def solve_path(self, targets: Iterable[Iterable[float]]) -> PathResult:
        """对整条目标序列求解并计算连续性 / 精度汇总指标。"""
        self.reset()
        result = PathResult()
        errors: list[float] = []
        last_feasible: Optional[tuple[float, float]] = None

        for i, tgt in enumerate(targets):
            sol = self.solve(tgt)
            pr = PathPointResult(
                index=i,
                target=(float(sol.target[0]), float(sol.target[1])),
                reachable=sol.reachable,
                status=sol.status,
                joints=sol.joints,
                chosen=sol.chosen,
                flipped=sol.flipped,
                position_error=sol.position_error,
                boundary_gap=sol.boundary_gap,
                singular=sol.singular,
                near_singular=sol.near_singular,
                note=sol.note,
            )
            result.points.append(pr)

            if sol.joints is None:
                result.failed_count += 1
                continue

            result.solved_count += 1
            if sol.flipped:
                result.flip_count += 1
            if sol.singular:
                result.singular_count += 1
            if sol.near_singular:
                result.near_singular_count += 1
            if sol.position_error is not None:
                errors.append(sol.position_error)
            if last_feasible is not None:
                step = math.hypot(
                    wrap_to_pi(sol.joints[0] - last_feasible[0]),
                    wrap_to_pi(sol.joints[1] - last_feasible[1]),
                )
                result.max_joint_step = (
                    step
                    if result.max_joint_step is None
                    else max(result.max_joint_step, step)
                )
            last_feasible = sol.joints

        if errors:
            result.max_position_error = max(errors)
            result.mean_position_error = sum(errors) / len(errors)
        return result
