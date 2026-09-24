"""端到端服务测试：夹具回归、失败回退、迭代预算、跨尺度不变性。"""

import json
from pathlib import Path

import numpy as np
import pytest

from app.geometry import validate_trajectory
from app.smoother import MAX_ITER_BUDGET, smooth_trajectory

EX = Path(__file__).resolve().parent.parent / "examples"


def load(name, **overrides):
    req = json.loads((EX / name).read_text(encoding="utf-8"))
    req.update(overrides)
    req["save_run"] = False
    return req


def endpoint_distance(pts):
    return float(np.hypot(pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1]))


@pytest.mark.parametrize("name", [
    "narrow_corridor.json",
    "angle_cutting.json",
    "duplicate_points.json",
])
def test_fixtures_succeed_and_are_valid(name):
    req = load(name)
    resp = smooth_trajectory(req)
    assert resp["success"], resp["reason"]
    pts = np.array(resp["points"])
    orig = np.array(req["points"])

    # 固定端点。
    assert np.allclose(pts[0], orig[0]) and np.allclose(pts[-1], orig[-1])
    # 点数不超过 100，且与输入等长（重复点恢复）。
    assert len(pts) == len(orig) <= 100

    m = resp["metrics"]
    rw = m["residuals_world"]
    # 目标函数真实下降（历史单调非增，允许 SLSQP 数值抖动 1e-8）。
    hist = m["objective_history"]
    assert hist[-1] <= hist[0] + 1e-8
    # 约束残差均满足。
    assert rw["deviation_max"] <= req["deviation_bound"] + 1e-7 * max(
        1.0, req["deviation_bound"]
    )
    assert rw["curvature_max"] <= req["max_curvature"] * (1 + 1e-6) + 1e-9
    if req["obstacles"]:
        assert rw["obstacle_sample_min_clearance"] >= req.get("safety_margin", 0) - 1e-9
    else:
        assert rw["obstacle_sample_min_clearance"] is None

    # 整段精确碰撞验证（线段内部也要查，不只是控制点）。
    min_clr, events = validate_trajectory(
        pts, [tuple(o) for o in req["obstacles"]], req.get("safety_margin", 0)
    )
    assert events == []
    if req["obstacles"]:
        assert min_clr >= req.get("safety_margin", 0) - 1e-9
        assert resp["min_clearance_world"] == pytest.approx(min_clr, abs=1e-9)
    else:
        assert np.isinf(min_clr)
        assert resp["min_clearance_world"] is None


def test_narrow_corridor_actually_smoothed():
    req = load("narrow_corridor.json")
    resp = smooth_trajectory(req)
    pts = np.array(resp["points"])
    # 平滑后曲率应明显小于原路径锯齿的最大曲率。
    from app.geometry import menger_curvature_sq
    orig = np.array(req["points"])

    def kmax(p):
        return max(
            np.sqrt(menger_curvature_sq(p[i - 1], p[i], p[i + 1]))
            for i in range(1, len(p) - 1)
        )

    assert kmax(pts) < 0.5 * kmax(orig)


def test_duplicate_points_preserved_in_output():
    req = load("duplicate_points.json")
    resp = smooth_trajectory(req)
    pts = np.array(resp["points"])
    assert len(pts) == 10
    assert np.allclose(pts[0], pts[1])           # 开头重复点保持
    assert np.allclose(pts[2], pts[3]) and np.allclose(pts[3], pts[4])
    assert np.allclose(pts[6], pts[7])


def test_scale_invariance_micro_macro():
    """微/宏观两个几何等价夹具：归一化目标值一致，世界残差按尺度缩放。"""
    micro = smooth_trajectory(load("scale_micro.json"))
    macro = smooth_trajectory(load("scale_macro.json"))
    assert micro["success"] and macro["success"]

    hm = micro["metrics"]["objective_history"]
    hM = macro["metrics"]["objective_history"]
    assert hm[-1] == pytest.approx(hM[-1], rel=1e-6, abs=1e-7)

    rm = micro["metrics"]["residuals_world"]
    rM = macro["metrics"]["residuals_world"]
    ratio = 1e7  # 3e-4 / 3000
    assert rM["deviation_max"] / rm["deviation_max"] == pytest.approx(ratio, rel=1e-4)
    assert rm["curvature_max"] / rM["curvature_max"] == pytest.approx(ratio, rel=1e-4)


def test_infeasible_curvature_returns_original():
    # 给一个极小曲率上界，折线无法在偏离预算内满足 -> 失败且返回原路径。
    req = load("narrow_corridor.json", max_curvature=1e-6, deviation_bound=0.05)
    resp = smooth_trajectory(req)
    assert resp["success"] is False
    assert resp["result"] == "original"
    assert np.array_equal(np.array(resp["points"]), np.array(req["points"]))
    assert resp["reason"] in (
        "solver_not_converged",
        "curvature_constraint_unsatisfied",
        "collision_after_solve",
        "infeasible",
    )
    # 绝不发布非法新路径：失败时无 metrics 字段。
    assert "metrics" not in resp


def test_original_path_in_collision_fails_original():
    req = load("narrow_corridor.json", safety_margin=10.0)
    resp = smooth_trajectory(req)
    assert resp["success"] is False
    assert resp["reason"] == "original_path_in_collision"
    assert np.array_equal(np.array(resp["points"]), np.array(req["points"]))


def test_zero_deviation_bound_keeps_points_when_feasible():
    req = load("duplicate_points.json", deviation_bound=0.0)
    # models 层要求 >0，直接走服务层验证退化情形不会产出新路径。
    resp = smooth_trajectory(req)
    orig = np.array(req["points"])
    if resp["success"]:
        pts = np.array(resp["points"])
        assert np.allclose(pts, orig, atol=1e-8)
    else:
        assert resp["result"] == "original"


def test_iteration_budget_respected():
    resp = smooth_trajectory(load("angle_cutting.json", max_iter=3))
    for run in resp["solve_runs"]:
        assert run["iterations"] <= 3
    assert sum(r["iterations"] for r in resp["solve_runs"]) <= 3
    assert resp["limits"]["max_iter"] == 3


def test_iteration_hard_limit_rejects_overbudget():
    with pytest.raises(Exception):
        smooth_trajectory(load("angle_cutting.json", max_iter=MAX_ITER_BUDGET + 1))


def test_too_many_points_rejected():
    req = {"points": [[i, 0.0] for i in range(101)],
           "obstacles": [], "deviation_bound": 1.0, "max_curvature": 1.0,
           "save_run": False}
    with pytest.raises(Exception):
        smooth_trajectory(req)


def test_response_is_data_not_fixed_curve():
    """解必须随数据变化：收紧偏离预算得到的偏离量必须更小（禁止固定平滑曲线冒充）。"""
    r_loose = smooth_trajectory(load("angle_cutting.json", deviation_bound=1.0))
    r_tight = smooth_trajectory(load("angle_cutting.json", deviation_bound=0.05))
    assert r_loose["success"]
    d_loose = r_loose["metrics"]["residuals_world"]["deviation_max"]
    if r_tight["success"]:
        d_tight = r_tight["metrics"]["residuals_world"]["deviation_max"]
        assert d_tight <= 0.05 + 1e-9
        assert d_tight < d_loose
    else:
        # 更紧预算不可行也允许，但必须如实返回原路径。
        assert r_tight["result"] == "original"


def test_run_artifact_persisted(tmp_path, monkeypatch):
    import app.smoother as sm
    monkeypatch.setattr(sm, "RUNS_DIR", tmp_path)
    resp = smooth_trajectory(load("duplicate_points.json") | {"save_run": True})
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text(encoding="utf-8"))
    assert rec["success"] is True
    # 目标函数历史与约束残差均已保存。
    runs = rec["response"]["solve_runs"]
    assert runs[0]["objective_history"]
    for key in ("deviation_max", "curvature_max", "obstacle_sample_min_clearance"):
        assert key in runs[0]["residuals_world"]
    assert resp["integrity_sha256"]
