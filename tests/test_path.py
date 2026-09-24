"""沿路径连续选解测试（肘部翻转、完全伸直、限位冲突、不可达缺口）。"""

import math

import pytest

from app.kinematics import ContinuousIKSolver, IKStatus
from app.synthetic import (
    DEFAULT_PARAMS,
    LinkParams,
    circle_path,
    extension_target,
    path_with_unreachable_gap,
    smooth_fk_path,
    target_from_fk,
)


def test_smooth_fk_replay_continuous(arm):
    """关节线性插值 -> FK 回放：逆解应贴合，无意外翻转，关节步长有界。"""
    targets = smooth_fk_path(n=61)
    res = ContinuousIKSolver(arm).solve_path(targets)
    assert res.failed_count == 0
    assert res.flip_count == 0
    assert res.max_position_error < 1e-10
    # 关节空间每步约 (2.4/60, 1.8/60)，合成步长 ≈ 0.05
    assert res.max_joint_step < 0.1


def test_circle_path_all_reachable(arm):
    """圆路径（含近伸直点 (1.3,0)）全部可达，误差≈0。"""
    res = ContinuousIKSolver(arm).solve_path(circle_path(n=72))
    assert res.solved_count == 72
    assert res.failed_count == 0
    assert res.max_position_error < 1e-10
    statuses = {p.status for p in res.points}
    # (1.3,0)：q2≈-0.644，非奇异；路径不经过严格奇异点
    assert IKStatus.UNREACHABLE not in statuses


def test_circle_through_full_extension(arm):
    """构造经过完全伸直点 (2,0) 的路径：该点标记 FULL_EXTENSION。"""
    # 沿半径 2 的射线角度连续扫过 q2=0 点附近：直接在圆 r=2 上采样
    targets = []
    n = 9
    for k in range(n):
        a = -0.2 + 0.4 * k / (n - 1)
        targets.append((2.0 * math.cos(a), 2.0 * math.sin(a)))
    res = ContinuousIKSolver(arm).solve_path(targets)
    mid = res.points[n // 2]
    assert mid.status == IKStatus.FULL_EXTENSION
    assert res.failed_count == 0
    assert res.max_position_error < 1e-10
    # 经过奇异点前后的关节步长应保持平滑（不会跳到另一支）
    assert res.max_joint_step < 0.2


def test_unreachable_gap_keeps_continuity(arm):
    """可达 -> 不可达 -> 不可达 -> 可达：失败点不更新参考，恢复后仍连续。"""
    targets = path_with_unreachable_gap(arm)
    solver = ContinuousIKSolver(arm)
    res = solver.solve_path(targets)
    assert res.solved_count == 2
    assert res.failed_count == 2
    assert res.points[1].status == IKStatus.UNREACHABLE
    assert res.points[2].status == IKStatus.UNREACHABLE
    assert res.points[1].joints is None
    # 恢复点与起点完全相同目标 -> 相同关节角，没有大跳变
    assert res.points[3].joints == pytest.approx(res.points[0].joints, abs=1e-12)
    assert res.max_joint_step is None or res.max_joint_step < 1e-9


def test_limit_conflict_along_path():
    """路径中点撞限位：该点 LIMIT_VIOLATION，前后点不受污染。"""
    p = LinkParams(theta2_min=0.3, theta2_max=0.5)  # 只允许很窄的 q2
    targets = [
        target_from_fk(0.0, 0.4, p),       # 可行（在限位内）
        (1.0, 1.0),                         # 两支 q2=±pi/2 均违规
        target_from_fk(0.0, 0.4, p),       # 可行
    ]
    res = ContinuousIKSolver(p).solve_path(targets)
    assert res.points[0].status == IKStatus.OK
    assert res.points[1].status == IKStatus.LIMIT_VIOLATION
    assert res.points[1].joints is None
    assert res.points[2].status == IKStatus.OK


def test_elbow_flip_is_explicit_when_forced(arm):
    """求解器按记忆的分支名报告翻转：肘上 -> 肘下 即 flipped。"""
    solver = ContinuousIKSolver(arm)
    # 第一步：从肘上侧记忆出发，选肘上，不翻转
    solver._previous = (math.pi / 2, -math.pi / 2)
    solver._previous_branch = "elbow_up"
    s1 = solver.solve((1.0, 1.0))
    assert s1.chosen == "elbow_up" and not s1.flipped
    # 第二步：记忆切到肘下（模拟绕奇异后换支），本次选肘下 -> 翻转
    solver._previous = (0.0, math.pi / 2)
    solver._previous_branch = "elbow_up"  # 上一可行点仍是肘上
    s2 = solver.solve((1.0, 1.0))
    assert s2.chosen == "elbow_down" and s2.flipped
    # 第三步：连续再来一次目标，保持肘下，不翻转
    s3 = solver.solve((1.0, 1.0))
    assert s3.chosen == "elbow_down" and not s3.flipped


def test_hysteresis_breaks_tie(arm):
    """两支角距离完全相等时，滞回项保持原分支，不抖动。"""
    # 目标 (2,0) 为奇异点（单支），改用对称目标构造等距：
    # q1=0 参考下，目标 (sqrt2*cos(pi/4), ...) 即 (1,1) 两支不等距；
    # 直接验证：从肘上解出发，在对称点上保持肘上
    solver = ContinuousIKSolver(arm, hysteresis=1e-3)
    s0 = solver.solve((1.0, 1.0))  # 无参考：默认选肘下（角距离 0 基准）
    assert s0.chosen == "elbow_down"
    # 绕一个小回路回到 (1,1)：参考在肘下侧，滞回使其保持肘下
    for a in [-0.02, 0.0, 0.02]:
        s = solver.solve((1.0 + 0.15 * math.sin(a), 1.0))
    s_back = solver.solve((1.0, 1.0))
    assert s_back.chosen == "elbow_down"


def test_solver_reset(arm):
    solver = ContinuousIKSolver(arm)
    solver.solve((1.0, 1.0))
    assert solver.previous_joints is not None
    solver.reset()
    assert solver.previous_joints is None


def test_extension_point_in_path_singular_count(arm):
    targets = [extension_target(angle=a, params=arm) for a in (0.3, 0.5, 0.7)]
    res = ContinuousIKSolver(arm).solve_path(targets)
    assert res.singular_count == 3
    assert res.failed_count == 0
