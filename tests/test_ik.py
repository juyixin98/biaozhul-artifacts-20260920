"""验收测试: 正解生成目标 -> 逆解回代, 覆盖肘部翻转/完全伸直/限位冲突/不可达。

运行: python -m pytest tests/ -v
"""

import math

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.kinematics import (
    ArmModel,
    IKStatus,
    JointLimits,
    forward_kinematics,
    inverse_kinematics,
    joint_distance,
    solve_path,
    wrap_to_pi,
)
from app.main import app

TOL = 1e-9
DEFAULT_ARM = ArmModel(l1=1.0, l2=1.0)


def fk_roundtrip_error(model: ArmModel, q: tuple[float, float]) -> float:
    """逆解 -> 正解回代的末端误差。"""
    x, y = forward_kinematics(model, *q)
    res = inverse_kinematics(model, x, y)
    errs = []
    for s in res.solutions:
        xr, yr = forward_kinematics(model, s.theta1, s.theta2)
        errs.append(math.hypot(xr - x, yr - y))
    return min(errs) if errs else math.inf


# ---------------------------------------------------------------- FK 基础

def test_fk_known_configuration():
    # theta1=0, theta2=0: 沿 x 轴伸直, 末端 (L1+L2, 0)
    x, y = forward_kinematics(DEFAULT_ARM, 0.0, 0.0)
    assert (x, y) == pytest.approx((2.0, 0.0), abs=TOL)
    # theta1=pi/2, theta2=0: 沿 y 轴
    x, y = forward_kinematics(DEFAULT_ARM, math.pi / 2, 0.0)
    assert (x, y) == pytest.approx((0.0, 2.0), abs=TOL)
    # theta1=0, theta2=pi/2: 肘部折起, 末端 (1, 1)
    x, y = forward_kinematics(DEFAULT_ARM, 0.0, math.pi / 2)
    assert (x, y) == pytest.approx((1.0, 1.0), abs=TOL)


# ------------------------------------------------------- 两支解与肘部翻转

def test_two_branches_both_reconstruct_target():
    """同一目标的两支解(肘上/肘下)正解回代都必须命中原目标。"""
    rng = np.random.default_rng(42)
    for _ in range(200):
        q = tuple(rng.uniform(-math.pi, math.pi, size=2))
        x, y = forward_kinematics(DEFAULT_ARM, *q)
        res = inverse_kinematics(DEFAULT_ARM, x, y)
        if res.singular:
            continue
        assert res.status == IKStatus.OK
        assert len(res.solutions) == 2
        branches = {s.branch for s in res.solutions}
        assert branches == {"elbow_down", "elbow_up"}
        for s in res.solutions:
            xr, yr = forward_kinematics(DEFAULT_ARM, s.theta1, s.theta2)
            assert math.hypot(xr - x, yr - y) < 1e-9


def test_elbow_flip_branches_are_distinct():
    """肘部翻转: 两支解的 theta2 符号相反, 且关节角确实不同。"""
    x, y = forward_kinematics(DEFAULT_ARM, 0.3, 0.8)
    res = inverse_kinematics(DEFAULT_ARM, x, y)
    down = next(s for s in res.solutions if s.branch == "elbow_down")
    up = next(s for s in res.solutions if s.branch == "elbow_up")
    assert down.theta2 > 0 > up.theta2
    assert down.theta2 == pytest.approx(-up.theta2, abs=1e-12)
    # 原关节角 (0.3, 0.8) 应被 elbow_down 支还原
    assert down.theta1 == pytest.approx(0.3, abs=1e-9)
    assert down.theta2 == pytest.approx(0.8, abs=1e-9)


def test_roundtrip_joint_recovery_random():
    """随机关节角 -> FK -> IK: 必有一支解在关节空间还原原角(模 2pi)。"""
    rng = np.random.default_rng(7)
    for _ in range(500):
        t1, t2 = rng.uniform(-math.pi, math.pi, size=2)
        if abs(math.sin(t2)) < 1e-6:  # 跳过奇异
            continue
        x, y = forward_kinematics(DEFAULT_ARM, t1, t2)
        res = inverse_kinematics(DEFAULT_ARM, x, y)
        assert res.status == IKStatus.OK
        recovered = any(
            abs(wrap_to_pi(s.theta1 - t1)) < 1e-9
            and abs(wrap_to_pi(s.theta2 - t2)) < 1e-9
            for s in res.solutions
        )
        assert recovered, f"q=({t1}, {t2}) not recovered"


# ------------------------------------------------------------ 完全伸直/奇异

def test_fully_extended_is_singular():
    """完全伸直: 目标在工作空间外边界, 两支退化为同一解, 状态 SINGULAR。"""
    res = inverse_kinematics(DEFAULT_ARM, 2.0, 0.0)  # d = L1+L2
    assert res.status == IKStatus.SINGULAR
    assert res.singular
    assert len(res.solutions) == 1
    s = res.solutions[0]
    assert s.theta1 == pytest.approx(0.0, abs=1e-9)
    assert s.theta2 == pytest.approx(0.0, abs=1e-9)


def test_fully_folded_is_singular():
    """完全折叠: L1==L2 时目标在原点, theta2=±pi 退化。"""
    res = inverse_kinematics(DEFAULT_ARM, 0.0, 0.0)
    assert res.status == IKStatus.SINGULAR
    assert len(res.solutions) == 1
    assert abs(abs(res.solutions[0].theta2) - math.pi) < 1e-9


def test_near_boundary_extension_roundtrip():
    """接近完全伸直(但未达)的目标仍可解且回代误差小。"""
    q = (0.5, 1e-4)
    x, y = forward_kinematics(DEFAULT_ARM, *q)
    res = inverse_kinematics(DEFAULT_ARM, x, y)
    assert res.status == IKStatus.OK
    assert fk_roundtrip_error(DEFAULT_ARM, q) < 1e-9


# ---------------------------------------------------------------- 不可达

def test_unreachable_beyond_reach():
    """超出最大臂展: 如实报 UNREACHABLE, 不裁剪坐标。"""
    res = inverse_kinematics(DEFAULT_ARM, 3.0, 0.0)
    assert res.status == IKStatus.UNREACHABLE
    assert res.solutions == ()
    assert res.reach_distance == pytest.approx(3.0)


def test_unreachable_inside_inner_radius():
    """小于最小半径 |L1-L2|: 内圈空洞不可达。"""
    arm = ArmModel(l1=2.0, l2=1.0)
    res = inverse_kinematics(arm, 0.5, 0.0)  # d=0.5 < |2-1|=1
    assert res.status == IKStatus.UNREACHABLE
    assert res.solutions == ()


# ------------------------------------------------------------- 关节限位

LIMITED = JointLimits(
    theta1_min=-math.pi / 2, theta1_max=math.pi / 2,
    theta2_min=-math.pi / 2, theta2_max=math.pi / 2,
)
LIMITED_ARM = ArmModel(l1=1.0, l2=1.0, limits=LIMITED)


def test_limit_conflict_both_branches_violate():
    """限位冲突: 几何可达但两支解都越限 -> LIMIT_VIOLATION, 不伪造成功。"""
    # 目标 (-0.9, 0.9): d≈1.273, |theta2|≈1.76 > pi/2, 两支解 theta2 都越限
    res = inverse_kinematics(LIMITED_ARM, -0.9, 0.9)
    assert res.status == IKStatus.LIMIT_VIOLATION
    assert all(not s.within_limits for s in res.solutions)
    # 解仍然附上, 供诊断
    assert len(res.solutions) == 2


def test_limit_one_branch_feasible():
    """一支越限一支合法: 状态 OK, 且合法支在限位内。"""
    # theta1=0.4, theta2=0.6 的目标: elbow_down 支在限位内
    x, y = forward_kinematics(LIMITED_ARM, 0.4, 0.6)
    res = inverse_kinematics(LIMITED_ARM, x, y)
    assert res.status == IKStatus.OK
    feasible = [s for s in res.solutions if s.within_limits]
    assert len(feasible) >= 1
    for s in feasible:
        assert LIMITED.contains((s.theta1, s.theta2))


def test_limit_boundary_inclusive():
    """恰好落在限位边界上的解应判为合法(闭区间)。"""
    x, y = forward_kinematics(LIMITED_ARM, math.pi / 2, math.pi / 2)
    res = inverse_kinematics(LIMITED_ARM, x, y)
    assert any(s.within_limits for s in res.solutions)


# ------------------------------------------------------- 路径连续选解

def test_path_continuity_no_branch_flip():
    """沿光滑路径选解: 相邻步关节跳变量小, 不发生肘部翻转。"""
    # 由一条 theta2 恒为正的关节轨迹生成路径
    n = 50
    t1s = np.linspace(-1.0, 1.0, n)
    t2s = np.linspace(0.5, 1.2, n)
    pts = [forward_kinematics(DEFAULT_ARM, a, b) for a, b in zip(t1s, t2s)]
    steps = solve_path(DEFAULT_ARM, pts)
    assert all(s["status"] == IKStatus.OK.value for s in steps)
    branches = {s["branch"] for s in steps}
    assert branches == {"elbow_down"}  # 全程不翻肘
    jumps = [s["step_jump"] for s in steps if s["step_jump"] is not None]
    assert len(jumps) == n - 1
    assert max(jumps) < 0.2  # 相邻步关节变化连续


def test_path_seed_selects_branch():
    """seed 决定首点选哪一支, 之后保持连续。"""
    pts = [forward_kinematics(DEFAULT_ARM, 0.3, 0.8)]
    down = solve_path(DEFAULT_ARM, pts, seed=(0.3, 0.8))
    up = solve_path(DEFAULT_ARM, pts, seed=(0.3, -0.8))
    assert down[0]["branch"] == "elbow_down"
    assert up[0]["branch"] == "elbow_up"


def test_path_through_unreachable_reports_and_recovers():
    """路径中途不可达: 该点如实标记, 不沿用旧关节角, 之后恢复求解。"""
    pts = [(1.0, 0.5), (5.0, 5.0), (1.0, -0.5)]
    steps = solve_path(DEFAULT_ARM, pts)
    assert steps[0]["status"] == IKStatus.OK.value
    assert steps[1]["status"] == IKStatus.UNREACHABLE.value
    assert steps[1]["theta1"] is None
    assert steps[2]["status"] == IKStatus.OK.value
    # 断链后重新起链: 第三点 step_jump 为 None
    assert steps[2]["step_jump"] is None


def test_path_joint_continuity_metric():
    """连续选解的关节跳变应显著小于固定选一支(错误支)时的跳变。"""
    n = 30
    t1s = np.linspace(0.2, 1.0, n)
    t2s = np.linspace(0.4, 1.0, n)
    pts = [forward_kinematics(DEFAULT_ARM, a, b) for a, b in zip(t1s, t2s)]
    steps = solve_path(DEFAULT_ARM, pts)
    total_jump = sum(s["step_jump"] for s in steps if s["step_jump"] is not None)
    # 若第二步起强制翻肘, 跳变量级为 2*|theta2| ~ 1.4/步
    assert total_jump < 1.0


# ------------------------------------------------------------ 数值稳健性

def test_wrap_to_pi():
    assert wrap_to_pi(3 * math.pi) == pytest.approx(math.pi)
    assert wrap_to_pi(-3 * math.pi) == pytest.approx(math.pi)
    assert wrap_to_pi(0.5) == pytest.approx(0.5)


def test_joint_distance_metric():
    assert joint_distance((0.0, 0.0), (0.0, 0.0)) == 0.0
    assert joint_distance((0.0, 0.0), (2 * math.pi, 0.0)) == pytest.approx(0.0)


def test_fk_ik_roundtrip_end_effector_error_random():
    """随机采样: 逆解回代末端误差 < 1e-9 (验收主指标)。"""
    rng = np.random.default_rng(123)
    errors = []
    for _ in range(1000):
        q = tuple(rng.uniform(-math.pi, math.pi, size=2))
        errors.append(fk_roundtrip_error(DEFAULT_ARM, q))
    errors = np.array(errors)
    assert np.all(errors < 1e-9), f"max error {errors.max()}"


# ------------------------------------------------------------ API 层

client = TestClient(app)


def test_api_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_api_fk_ik_roundtrip():
    r = client.post("/fk", json={"theta1": 0.4, "theta2": 0.7})
    assert r.status_code == 200
    pos = r.json()
    r = client.post("/ik", json={"x": pos["x"], "y": pos["y"]})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert len(body["solutions"]) == 2
    for s in body["solutions"]:
        rr = client.post("/fk", json={"theta1": s["theta1"], "theta2": s["theta2"]})
        p = rr.json()
        assert math.hypot(p["x"] - pos["x"], p["y"] - pos["y"]) < 1e-9


def test_api_ik_unreachable():
    r = client.post("/ik", json={"x": 10.0, "y": 0.0})
    assert r.status_code == 200
    assert r.json()["status"] == "unreachable"
    assert r.json()["solutions"] == []


def test_api_ik_limit_violation():
    r = client.post(
        "/ik",
        json={
            "x": -0.9,
            "y": 0.9,
            "arm": {
                "l1": 1.0,
                "l2": 1.0,
                "limits": {
                    "theta1_min": -math.pi / 2,
                    "theta1_max": math.pi / 2,
                    "theta2_min": -math.pi / 2,
                    "theta2_max": math.pi / 2,
                },
            },
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "limit_violation"


def test_api_path_endpoint():
    pts = []
    for t1, t2 in zip(np.linspace(-1, 1, 10), np.linspace(0.5, 1.2, 10)):
        pts.append(list(forward_kinematics(DEFAULT_ARM, t1, t2)))
    r = client.post("/ik/path", json={"points": pts})
    assert r.status_code == 200
    steps = r.json()["steps"]
    assert len(steps) == 10
    assert all(s["status"] == "ok" for s in steps)
    assert all(s["branch"] == "elbow_down" for s in steps)


def test_api_rejects_bad_input():
    # httpx 的 json 编码器拒绝 NaN, 用原始 JSON 体发送
    r = client.post(
        "/ik",
        content='{"x": NaN, "y": 0.0}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422
    r = client.post("/ik/path", json={"points": []})
    assert r.status_code == 422
