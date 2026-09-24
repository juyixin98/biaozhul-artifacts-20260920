"""逆运动学解析解、奇异、不可达、关节限位测试。"""

import math

import numpy as np
import pytest

from app.kinematics import (
    IKStatus,
    LinkParams,
    forward_kinematics,
    inverse_kinematics,
    jacobian,
    representative,
    wrap_to_pi,
)
from app.synthetic import (
    DEFAULT_PARAMS,
    both_elbow_targets,
    extension_target,
    fold_target,
    target_from_fk,
    unreachable_targets,
)


# ---------- FK -> IK 往返 ----------


@pytest.mark.parametrize("seed", range(10))
def test_fk_ik_roundtrip_random(arm, seed):
    """正解生成的目标必须能被逆解还原（验收核心）。"""
    rng = np.random.default_rng(seed)
    for _ in range(200):
        q1 = float(rng.uniform(-math.pi + 0.01, math.pi - 0.01))
        q2 = float(rng.uniform(-math.pi + 1e-4, math.pi - 1e-4))
        target = target_from_fk(q1, q2, arm)
        sol = inverse_kinematics(target, arm)
        assert sol.status in (IKStatus.OK, IKStatus.NEAR_SINGULAR)
        assert sol.joints is not None
        assert sol.position_error < 1e-10
        # 还原关节角（允许另一支：用正解位置等价判定）
        ee = forward_kinematics(sol.joints[0], sol.joints[1], arm)
        assert ee == pytest.approx(target, abs=1e-10)


def test_both_analytic_branches_same_target(arm):
    """同一目标存在两支解析解：肘部上 / 下。"""
    cases = both_elbow_targets(arm)
    target = cases[0][2]
    sol = inverse_kinematics(target, arm)
    names = {b.name for b in sol.branches}
    assert names == {"elbow_up", "elbow_down"}
    up = next(b for b in sol.branches if b.name == "elbow_up")
    down = next(b for b in sol.branches if b.name == "elbow_down")
    assert up.q2 == pytest.approx(-math.pi / 2, abs=1e-12)
    assert down.q2 == pytest.approx(math.pi / 2, abs=1e-12)
    assert up.q1 == pytest.approx(math.pi / 2, abs=1e-12)
    assert down.q1 == pytest.approx(0.0, abs=1e-12)
    assert up.cross < 0.0 < down.cross
    assert up.error < 1e-12 and down.error < 1e-12


def test_explicit_preference_selects_branch(arm):
    sol = inverse_kinematics((1.0, 1.0), arm, preference="elbow_up")
    assert sol.chosen == "elbow_up"
    assert sol.joints[1] < 0.0
    sol = inverse_kinematics((1.0, 1.0), arm, preference="elbow_down")
    assert sol.chosen == "elbow_down"
    assert sol.joints[1] > 0.0


def test_preference_with_continuity_reference(arm):
    """previous_joints 应选择角距离更近的一支。"""
    # 从肘下支附近出发（q1≈0, q2>0）
    sol = inverse_kinematics(
        (1.0, 1.0), arm, previous_joints=(0.0, math.pi / 2)
    )
    assert sol.chosen == "elbow_down"
    # 从肘上支附近出发
    sol = inverse_kinematics(
        (1.0, 1.0), arm, previous_joints=(math.pi / 2, -math.pi / 2)
    )
    assert sol.chosen == "elbow_up"


# ---------- 肘部翻转 ----------


def test_elbow_flip_flag(arm):
    sol1 = inverse_kinematics(
        (1.0, 1.0), arm, previous_joints=(0.0, math.pi / 2)
    )
    assert sol1.chosen == "elbow_down" and not sol1.flipped
    sol2 = inverse_kinematics(
        (1.0, 1.0),
        arm,
        previous_joints=(math.pi / 2, -math.pi / 2),
    )
    # 参考为肘上、选择结果仍为肘上（连续性），不翻转
    assert sol2.chosen == "elbow_up" and not sol2.flipped


# ---------- 完全伸直奇异 ----------


def test_full_extension_singular(arm):
    target = extension_target(angle=math.pi / 4, params=arm)
    sol = inverse_kinematics(target, arm)
    assert sol.status == IKStatus.FULL_EXTENSION
    assert sol.singular and not sol.near_singular
    assert sol.joints is not None
    assert sol.joints[1] == pytest.approx(0.0, abs=1e-12)
    assert sol.joints[0] == pytest.approx(math.pi / 4, abs=1e-12)
    assert sol.position_error < 1e-12
    assert len(sol.branches) == 1
    assert sol.branches[0].name == "degenerate"
    # 雅可比奇异
    assert abs(np.linalg.det(jacobian(*sol.joints, arm))) < 1e-12


def test_near_singular_classification(arm):
    """刚好完全伸直邻域：near_singular 而非 full_extension。

    q2=3e-6 时 r=2cos(q2/2) 与外边界差距约 4.5e-12，远大于边界容差
    2e-9，因此不是严格奇异；但 |sin(q2)| > singular_eps=1e-6，
    故也不算近奇异——这里取 q2=3e-7 验证 NEAR_SINGULAR 分支。
    """
    q2 = 3e-7  # |sin| < singular_eps=1e-6，且 r 与边界差距 < 容差带外
    target = target_from_fk(0.0, q2, arm)
    sol = inverse_kinematics(target, arm)
    assert sol.status == IKStatus.NEAR_SINGULAR
    assert sol.near_singular and not sol.singular


# ---------- 完全折叠奇异 ----------


def test_full_fold_at_origin(arm):
    target = fold_target(arm)  # (0,0)
    sol = inverse_kinematics(target, arm)
    assert sol.status == IKStatus.FULL_FOLD
    assert sol.singular
    assert sol.joints is not None
    assert sol.joints[1] == pytest.approx(-math.pi, abs=1e-12)
    assert sol.position_error < 1e-12


# ---------- 不可达：绝不裁剪坐标 ----------


@pytest.mark.parametrize("target", [(3.0, 0.0), (0.0, 3.0), (2.0, 2.0)])
def test_unreachable_outside(arm, target):
    sol = inverse_kinematics(target, arm)
    assert sol.status == IKStatus.UNREACHABLE
    assert sol.joints is None
    assert sol.end_effector is None
    assert sol.boundary_gap == pytest.approx(
        math.hypot(*target) - arm.reach, rel=1e-9
    )
    assert not sol.reachable


def test_unreachable_inner_hole(uneven_arm):
    """l1=1.2,l2=0.8：r<0.4 为内部空洞。"""
    sol = inverse_kinematics((0.2, 0.0), uneven_arm)
    assert sol.status == IKStatus.UNREACHABLE
    assert sol.joints is None
    assert sol.boundary_gap == pytest.approx(0.2, abs=1e-12)


def test_no_coordinate_clamping(arm):
    """不可达目标返回 None，而不是返回边界上的“成功”点。"""
    bad = (arm.reach * 1.5, arm.reach * 1.5)
    sol = inverse_kinematics(bad, arm)
    assert sol.joints is None
    assert sol.status == IKStatus.UNREACHABLE
    assert "success" not in sol.as_dict() or sol.as_dict()["joints"] is None


def test_unreachable_targets_suite(arm):
    for t in unreachable_targets(arm):
        assert inverse_kinematics(t, arm).status == IKStatus.UNREACHABLE


# ---------- 关节限位 ----------


def test_limit_violation_when_both_branches_blocked():
    """theta2 限定为 [0.5, pi]：目标 (1,1) 肘上支 q2=-pi/2 违规，
    肘下支 q2=+pi/2 可行，自动选可行支。"""
    p = LinkParams(theta2_min=0.5, theta2_max=math.pi)
    sol = inverse_kinematics((1.0, 1.0), p)
    assert sol.status == IKStatus.OK
    assert sol.chosen == "elbow_down"


def test_limit_violation_all_branches_blocked():
    """theta2 限定为 [0.3, 0.5]：两支 q2=±pi/2 均违规。"""
    p = LinkParams(theta2_min=0.3, theta2_max=0.5)
    sol = inverse_kinematics((1.0, 1.0), p)
    assert sol.status == IKStatus.LIMIT_VIOLATION
    assert sol.joints is None
    assert all(not b.feasible for b in sol.branches)
    assert all(b.violations for b in sol.branches)
    # 分支明细仍透明返回
    assert len(sol.branches) == 2


def test_preference_blocked_reports_limit_violation(elbow_up_only_arm):
    """显式偏好肘下，但限位只允许肘上：必须报 LIMIT_VIOLATION，不静默换支。"""
    sol = inverse_kinematics(
        (1.0, 1.0), elbow_up_only_arm, preference="elbow_down"
    )
    assert sol.status == IKStatus.LIMIT_VIOLATION
    assert sol.joints is None


def test_preference_blocked_auto_picks_other(elbow_up_only_arm):
    """无显式偏好时，自动选择满足限位的另一支。"""
    sol = inverse_kinematics((1.0, 1.0), elbow_up_only_arm)
    assert sol.status == IKStatus.OK
    assert sol.chosen == "elbow_up"


def test_limit_range_wrapping_across_pi():
    """限位区间跨越 ±pi：应通过 2*pi 等价表示判定。"""
    # q2=3.0 包裹表示 3.0-2pi=-3.283；限位 [-4,-2] 可容纳
    assert representative(3.0, -4.0, -2.0) == pytest.approx(3.0 - 2 * math.pi)
    assert representative(0.0, 1.0, 2.0) is None
    p = LinkParams(theta2_min=-4.0, theta2_max=-2.0)
    # q2 ≈ -pi±0.14 落在区间内的可达目标
    target = target_from_fk(0.0, -3.0, DEFAULT_PARAMS)
    sol = inverse_kinematics(target, p)
    assert sol.joints is not None
    assert -4.0 <= sol.joints[1] <= -2.0


# ---------- 输入校验 ----------


def test_bad_target_shape(arm):
    with pytest.raises(ValueError):
        inverse_kinematics((1.0, 2.0, 3.0), arm)


def test_bad_preference(arm):
    with pytest.raises(ValueError):
        inverse_kinematics((1.0, 1.0), arm, preference="sideways")
