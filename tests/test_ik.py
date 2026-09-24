"""逆运动学主链路测试：往返、正解核验保证、三分类、多解择优、初值变化。"""

import dataclasses

import numpy as np
import pytest

from app.core.angles import angular_distance, canonicalize_to_limits
from app.core.ik import IKStatus


POS_TOL = 1e-4
ORI_TOL = 1e-4


def assert_success_verified(result, robot, T):
    """任何成功都必须真的通过独立 FK 核验（不可只信迭代残差）。"""
    assert result.status == IKStatus.SUCCESS
    assert result.q is not None
    assert np.all(result.q >= robot.joint_lower - 1e-7)
    assert np.all(result.q <= robot.joint_upper + 1e-7)
    assert result.position_error <= POS_TOL
    assert result.orientation_error <= ORI_TOL
    # 用返回关节角重新做 FK，与目标独立核对一遍
    Tcheck = robot.fk(result.q)
    np.testing.assert_allclose(Tcheck[:3, 3], T[:3, 3], atol=POS_TOL)
    np.testing.assert_allclose(Tcheck[:3, :3], T[:3, :3], atol=ORI_TOL)


def test_known_forward_solutions_roundtrip(robot, solver_cfg, ik_solve):
    """已知正解生成的目标必须逆解成功并通过核验（确定性种子）。"""
    rng = np.random.default_rng(20260923)
    n = 12
    for t in range(n):
        q = robot.joint_lower + rng.random(6) * (robot.joint_upper - robot.joint_lower)
        T = robot.fk(q)
        result = ik_solve(T, current=np.zeros(6))
        assert_success_verified(result, robot, T)
        assert len(result.candidates) >= 1


def test_roundtrip_includes_near_generating_branch(robot, ik_solve):
    """至少有一个核验候选落在生成构型附近（周期距离），证明解的物理正确性。"""
    q = np.array([0.7, -0.5, 0.9, 0.6, 0.4, 0.8])
    T = robot.fk(q)
    r = ik_solve(T, current=np.zeros(6))
    assert r.status == IKStatus.SUCCESS
    ds = [
        float(np.linalg.norm(angular_distance(c.q, q)))
        for c in r.candidates
    ]
    assert min(ds) < 0.2


def test_no_success_without_fk_verification(robot, solver_cfg, fk):
    """手工把核验容差收紧到不可能达到时，绝不允许返回 SUCCESS。"""
    strict = dataclasses.replace(
        solver_cfg,
        verify_position_tolerance=1e-14,
        verify_orientation_tolerance=1e-14,
        position_tolerance=1e-13,
        orientation_tolerance=1e-13,
        max_iterations=80,
    )
    from app.core.ik import solve_ik

    q = np.array([0.4, -0.4, 0.7, 0.3, 0.3, 0.2])
    T = fk(q)
    r = solve_ik(robot, strict, T, current_q=np.zeros(6))
    assert r.status != IKStatus.SUCCESS
    assert r.q is None


def test_unreachable_target(robot, ik_solve):
    """超工作空间目标 → UNREACHABLE（10 m 与略超边界都测）。"""
    T = np.eye(4)
    T[0, 3] = 10.0
    r = ik_solve(T)
    assert r.status == IKStatus.UNREACHABLE
    assert r.diagnostics["wrist_requested_radius"] > r.diagnostics["wrist_max_reach"]

    # 刚刚越过伸直边界：沿真实伸直构型 approach 外推
    delta = np.arctan2(robot.d[3], robot.a[2])
    Ts = robot.fk(np.array([0.0, 0.0, delta, 0.0, 0.0, 0.0]))
    Te = Ts.copy()
    Te[:3, 3] = Ts[:3, 3] + 5e-4 * Ts[:3, 2]
    re = ik_solve(Te)
    assert re.status == IKStatus.UNREACHABLE


def test_extended_singularity_exact_target(robot, ik_solve):
    """伸直奇异位姿的精确目标：要么成功（零初速度穿过奇异），要么
    SINGULAR_NO_CONVERGE；绝不允许误报 UNREACHABLE，也不允许未核验成功。"""
    delta = np.arctan2(robot.d[3], robot.a[2])
    for q5 in (0.0, 0.5, 1.0):
        q = np.array([0.4, 0.0, delta, 0.0, q5, 0.0])
        T = robot.fk(q)
        r = ik_solve(T, current=np.zeros(6))
        assert r.status in (IKStatus.SUCCESS, IKStatus.SINGULAR_NO_CONVERGE)
        if r.status == IKStatus.SUCCESS:
            assert_success_verified(r, robot, T)
        else:
            # 预检必须仍判可达
            assert r.diagnostics["precheck"] == "reachable"


def test_singular_non_convergence_with_limited_budget(robot, solver_cfg):
    """有限迭代预算下、几何可达目标不收敛 → SINGULAR_NO_CONVERGE；
    恢复预算后必须成功（证明分类是“预算/奇异”问题而非不可达）。"""
    from app.core.ik import solve_ik

    q = np.array([-1.4, -1.1, 1.7, 2.3, -1.1, 4.2])
    T = robot.fk(q)
    tight = dataclasses.replace(
        solver_cfg, max_iterations=3, singular_stall_iterations=10
    )
    rt = solve_ik(robot, tight, T, current_q=np.zeros(6))
    assert rt.status == IKStatus.SINGULAR_NO_CONVERGE
    assert rt.diagnostics["precheck"] == "reachable"
    rf = solve_ik(robot, solver_cfg, T, current_q=np.zeros(6))
    assert_success_verified(rf, robot, T)


def test_limit_conflict_q1(robot, ik_solve):
    """q1=3.1 rad（限位 ±2.967）的 FK 目标 → LIMIT_CONFLICT，
    且诊断指出关节 1；不能误报 UNREACHABLE（腕点在工作空间内）。"""
    q = np.array([3.1, -0.3, 0.5, 0.2, 0.3, 0.1])
    T = robot.fk(q)
    r = ik_solve(T)
    assert r.status == IKStatus.LIMIT_CONFLICT
    assert 1 in r.diagnostics.get("offending_joints", [])
    assert r.diagnostics["precheck"] != "out_of_workspace"
    assert r.diagnostics["unconstrained_solutions"] >= 1


def test_three_failure_classes_distinct(robot, solver_cfg, ik_solve):
    """同一服务连续调用，三类失败必须可区分。"""
    from app.core.ik import solve_ik

    Tfar = np.eye(4)
    Tfar[0, 3] = 5.0
    s_far = ik_solve(Tfar).status

    qlim = np.array([3.12, 0.0, 0.0, 0.0, 0.0, 0.0])
    s_lim = ik_solve(robot.fk(qlim)).status

    tight = dataclasses.replace(
        solver_cfg, max_iterations=3, singular_stall_iterations=10
    )
    qs = np.array([-1.4, -1.1, 1.7, 2.3, -1.1, 4.2])
    s_sin = solve_ik(robot, tight, robot.fk(qs), current_q=np.zeros(6)).status

    assert {s_far, s_lim, s_sin} == {
        IKStatus.UNREACHABLE,
        IKStatus.LIMIT_CONFLICT,
        IKStatus.SINGULAR_NO_CONVERGE,
    }


def test_periodic_multi_solution_selection(robot, ik_solve):
    """同一目标、不同当前姿态 → 选不同周期支（普通差值会选错）。"""
    q_gen = np.array([0.6, -0.4, 0.8, 0.5, 0.3, 5.5])
    T = robot.fk(q_gen)
    cur_neg = np.array([0.5, -0.3, 0.7, 0.4, 0.2, -0.78])
    cur_pos = np.array([0.5, -0.3, 0.7, 0.4, 0.2, 5.5])

    rn = ik_solve(T, current=cur_neg)
    rp = ik_solve(T, current=cur_pos)
    assert_success_verified(rn, robot, T)
    assert_success_verified(rp, robot, T)
    assert rn.q[5] < 0.0
    assert rp.q[5] > 5.0
    # 两个返回都是同一末端位姿
    np.testing.assert_allclose(robot.fk(rn.q)[:3, 3], robot.fk(rp.q)[:3, 3], atol=1e-4)
    # 周期距离择优指标合理
    assert rp.candidates[0].joint_distance <= 0.3


def test_seed_variation_changes_outcomes(robot, solver_cfg):
    """初值变化：提供生成构型作为额外初值时，返回解应落在该构型附近。"""
    from app.core.ik import solve_ik

    q_gen = np.array([0.9, -0.9, 1.2, -0.8, 0.8, 1.5])
    T = robot.fk(q_gen)
    r0 = solve_ik(robot, solver_cfg, T, current_q=np.zeros(6))
    r1 = solve_ik(robot, solver_cfg, T, current_q=q_gen, extra_seeds=[q_gen])
    assert r0.status == IKStatus.SUCCESS and r1.status == IKStatus.SUCCESS
    d_with_seed = float(np.linalg.norm(angular_distance(r1.q, q_gen)))
    assert d_with_seed < 0.05


def test_joint_limits_always_respected(robot, ik_solve):
    """成功解逐轴在限位内（已含周期平移）。"""
    rng = np.random.default_rng(99)
    for _ in range(10):
        q = robot.joint_lower + rng.random(6) * (robot.joint_upper - robot.joint_lower)
        r = ik_solve(robot.fk(q), current=np.zeros(6))
        assert r.status == IKStatus.SUCCESS
        assert np.all(r.q >= robot.joint_lower - 1e-8)
        assert np.all(r.q <= robot.joint_upper + 1e-8)
