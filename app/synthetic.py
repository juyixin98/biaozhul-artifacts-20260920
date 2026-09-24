"""合成数据 / 离线回放：所有目标均由正运动学生成，不接真实硬件。

提供验收用的固定场景：
- 常规两支可达（肘部上 / 下翻转）
- 完全伸直（外边界奇异）
- 等长臂完全折叠（原点奇异）
- 关节限位冲突
- 不可达点
- 沿圆周 / 直线的连续路径（含经过伸直点、绕进工作区空洞外的路径）
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from .kinematics import LinkParams, forward_kinematics

DEFAULT_PARAMS = LinkParams(l1=1.0, l2=1.0)
"""验收默认臂：等长 1.0，关节限位 ±pi。"""

LIMITED_PARAMS = LinkParams(
    l1=1.0,
    l2=1.0,
    theta1_min=-math.pi,
    theta1_max=math.pi,
    # 只允许肘部“向上”（q2 <= -0.2），制造肘部翻转冲突
    theta2_min=-math.pi,
    theta2_max=-0.2,
)


def target_from_fk(
    q1: float, q2: float, params: LinkParams = DEFAULT_PARAMS
) -> Tuple[float, float]:
    """由关节角经正解生成目标点（合成数据唯一来源）。"""
    p = forward_kinematics(q1, q2, params)
    return float(p[0]), float(p[1])


def both_elbow_targets(
    params: LinkParams = DEFAULT_PARAMS,
) -> List[Tuple[float, float, Tuple[float, float]]]:
    """同一目标点对应的两支解析解（肘上 / 肘下）。

    返回 [(q1_up, q2_up, target), (q1_down, q2_down, target)]，
    两个 target 完全相同。
    """
    # 目标 (1,1)：r=sqrt(2), phi=pi/4, alpha=beta=pi/2/pi/4
    #   肘上支：q1=phi+beta=pi/2, q2=-alpha=-pi/2（肘部在 (0,1)，叉积<0）
    #   肘下支：q1=phi-beta=0,     q2=+alpha=+pi/2（肘部在 (1,0)，叉积>0）
    q1_up, q2_up = math.pi / 2, -math.pi / 2
    q1_down, q2_down = 0.0, math.pi / 2
    t_up = target_from_fk(q1_up, q2_up, params)
    t_down = target_from_fk(q1_down, q2_down, params)
    assert abs(t_up[0] - t_down[0]) < 1e-12
    assert abs(t_up[1] - t_down[1]) < 1e-12
    return [
        (q1_up, q2_up, t_up),
        (q1_down, q2_down, t_down),
    ]


def extension_target(
    angle: float = math.pi / 4, params: LinkParams = DEFAULT_PARAMS
) -> Tuple[float, float]:
    """完全伸直目标：q2=0，r=l1+l2。"""
    return target_from_fk(angle, 0.0, params)


def fold_target(params: LinkParams = DEFAULT_PARAMS) -> Tuple[float, float]:
    """完全折叠目标：等长臂 q2=±pi，末端在原点。"""
    return target_from_fk(0.3, math.pi, params)


def unreachable_targets(params: LinkParams = DEFAULT_PARAMS) -> List[Tuple[float, float]]:
    """明确不可达的合成目标（超出外边界；不要求由 FK 生成，因为不可达本就无 FK 对应）。"""
    r = params.reach
    return [(r * 1.2, 0.0), (0.0, r * 1.5), (r, r)]


def circle_path(
    center: Tuple[float, float] = (0.5, 0.0),
    radius: float = 0.8,
    n: int = 72,
    params: LinkParams = DEFAULT_PARAMS,
) -> List[Tuple[float, float]]:
    """合成圆形路径（参数方程采样；不经过 FK，但保证在默认臂工作区内）。

    默认圆心 (0.5, 0)、半径 0.8：经过 (1.3, 0) 近伸直点和 (-0.3, 0)，
    覆盖肘部翻转与近奇异区域。
    """
    pts = []
    for k in range(n):
        a = 2.0 * math.pi * k / n
        pts.append((center[0] + radius * math.cos(a), center[1] + radius * math.sin(a)))
    # 校验合成路径确实在工作区内（诚实原则：不偷偷构造不可达“成功”）
    for x, y in pts:
        rr = math.hypot(x, y)
        if rr > params.reach * (1.0 + params.reach_tol_ratio):
            raise ValueError(
                f"合成圆路径出现不可达点 ({x:.3f},{y:.3f}), r={rr:.3f}"
            )
    return pts


def fk_generated_path(
    q1_series: Sequence[float],
    q2_series: Sequence[float],
    params: LinkParams = DEFAULT_PARAMS,
) -> List[Tuple[float, float]]:
    """由两列关节角序列经正解回放生成目标路径（离线回放）。"""
    if len(q1_series) != len(q2_series):
        raise ValueError("q1_series 与 q2_series 长度必须一致")
    return [target_from_fk(a, b, params) for a, b in zip(q1_series, q2_series)]


def smooth_fk_path(
    q1_endpoints: Tuple[float, float] = (-1.2, 1.2),
    q2_endpoints: Tuple[float, float] = (-2.2, -0.4),
    n: int = 61,
    params: LinkParams = DEFAULT_PARAMS,
) -> List[Tuple[float, float]]:
    """关节空间线性插值 -> FK 回放，生成一条平滑的合成路径。"""
    q1a, q1b = q1_endpoints
    q2a, q2b = q2_endpoints
    q1s = [q1a + (q1b - q1a) * k / (n - 1) for k in range(n)]
    q2s = [q2a + (q2b - q2a) * k / (n - 1) for k in range(n)]
    return fk_generated_path(q1s, q2s, params)


def path_with_unreachable_gap(
    params: LinkParams = DEFAULT_PARAMS,
) -> List[Tuple[float, float]]:
    """可达 → 不可达 → 可达的回放序列，验证失败点不污染连续性基准。"""
    return [
        target_from_fk(0.0, -math.pi / 2, params),  # (1, 1)
        (params.reach * 1.4, 0.3),  # 不可达
        (params.reach * 1.5, 0.0),  # 不可达
        target_from_fk(0.0, -math.pi / 2, params),  # 回到 (1,1)
    ]


@dataclass(frozen=True)
class AcceptanceCase:
    """验收用例描述。"""

    name: str
    description: str
    targets: List[Tuple[float, float]]
    expect: str


def acceptance_suite(params: LinkParams = DEFAULT_PARAMS) -> List[AcceptanceCase]:
    """固定验收场景集合（合成数据 / 离线回放）。"""
    return [
        AcceptanceCase(
            name="elbow_flip",
            description="同一目标 (1,1) 的两支解析解：肘部上/下翻转",
            targets=[t for _, _, t in both_elbow_targets(params)[:1]],
            expect="两支均可达，末端误差≈0",
        ),
        AcceptanceCase(
            name="full_extension",
            description="完全伸直：q2=0，目标在外边界 r=l1+l2",
            targets=[extension_target(params=params)],
            expect="FULL_EXTENSION 奇异，单支退化解，误差≈0",
        ),
        AcceptanceCase(
            name="full_fold",
            description="等长臂完全折叠：末端在原点",
            targets=[fold_target(params)],
            expect="FULL_FOLD 奇异，q1 不可辨识",
        ),
        AcceptanceCase(
            name="unreachable",
            description="超出工作区外边界的目标",
            targets=unreachable_targets(params),
            expect="UNREACHABLE + boundary_gap，joints=None，不裁剪坐标",
        ),
        AcceptanceCase(
            name="circle_continuous",
            description="合成圆路径连续选解（近伸直、肘部翻转）",
            targets=circle_path(params=params),
            expect="连续选支，关节跳变有界",
        ),
        AcceptanceCase(
            name="fk_replay_smooth",
            description="关节插值经 FK 回放的平滑路径",
            targets=smooth_fk_path(params=params),
            expect="末端误差≈0，无意外翻转",
        ),
        AcceptanceCase(
            name="unreachable_gap",
            description="路径中插入不可达点后恢复",
            targets=path_with_unreachable_gap(params),
            expect="失败点不更新参考，恢复后仍连续",
        ),
    ]
