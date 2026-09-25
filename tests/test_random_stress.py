"""随机差分 / 数值压力测试。

* 随机生成含等式、上下界的小中规模问题，每个结果都必须通过独立核验：
  最优解可行；无界射线满足方向条件；不可行证书通过 Farkas 检查。
* 中等规模（接近声明上限）问题检查迭代次数与耗时可控。
* 与 scipy 无关——交叉验证来自测试内置的枚举器或直接的线性核验。
"""

from __future__ import annotations

import time

import numpy as np
from blp import make_lp, solve_lp
from blp.model import TOL
from blp.verify import ray_residuals, solution_residuals, verify_certificate

from helpers import canonical_arrays


def _random_problem(rng):
    n = int(rng.integers(2, 7))
    m_ub = int(rng.integers(0, 6))
    m_eq = int(rng.integers(0, 3))
    A_ub = rng.integers(-4, 5, size=(m_ub, n)).tolist() if m_ub else None
    b_ub = rng.integers(-3, 10, size=m_ub).tolist() if m_ub else None
    A_eq = rng.integers(-3, 4, size=(m_eq, n)).tolist() if m_eq else None
    b_eq = rng.integers(-3, 6, size=m_eq).tolist() if m_eq else None
    if rng.random() < 0.6:
        ub = rng.integers(1, 9, size=n).tolist()
    else:
        ub = None
    if rng.random() < 0.3:
        lb = rng.integers(0, 3, size=n).tolist()
    else:
        lb = None
    c = rng.integers(-5, 6, size=n).tolist()
    sense = "max" if rng.random() < 0.3 else "min"
    return dict(c=c, sense=sense, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq,
                b_eq=b_eq, ub=ub, lb=lb)


def test_random_problems_all_results_verified():
    rng = np.random.default_rng(1234)
    counts = {"optimal": 0, "infeasible": 0, "unbounded": 0}
    for _ in range(400):
        kw = _random_problem(rng)
        lp = make_lp(**kw)
        r = solve_lp(lp)
        assert r.status in ("optimal", "infeasible", "unbounded",
                            "numeric_failure", "iteration_limit"), r.status
        # 本规模下不应出现数值失败或超限（若出现必须暴露）。
        assert r.status not in ("numeric_failure", "iteration_limit"), (
            kw, r.status, r.detail
        )
        counts[r.status] += 1
        A_le, b_le, A_eq, b_eq, eff_ub = canonical_arrays(lp)
        if r.status == "optimal":
            y = r.x - lp.shift
            res = solution_residuals(y, A_le, b_le, A_eq, b_eq, eff_ub)
            assert res["feasible"], (kw, res)
            # 目标口径一致性。
            assert abs(r.objective - (float(lp.c @ r.x) + lp.c0)) < 1e-7
        elif r.status == "unbounded":
            y0 = r.x - lp.shift
            res = ray_residuals(y0, r.ray, A_le, b_le, A_eq, b_eq, eff_ub)
            assert res["valid"], (kw, res)
            rate = float(np.asarray(kw["c"], float) @ r.ray)
            if kw["sense"] == "min":
                assert rate < -TOL.reduced
            else:
                assert rate > TOL.reduced
        else:
            chk = verify_certificate(
                r.certificate["rows"], A_le, b_le, A_eq, b_eq, eff_ub
            )
            assert chk["valid"], (kw, chk)
    # 400 题里三种状态都应出现（否则测试覆盖不够）。
    assert counts["optimal"] > 100
    assert counts["infeasible"] > 5
    assert counts["unbounded"] > 5


def test_certificate_agrees_with_feasibility_status():
    """构造带已知不可行约束的问题，证书乘子给出的矛盾常数严格为负。"""
    rng = np.random.default_rng(99)
    found = 0
    for _ in range(300):
        kw = _random_problem(rng)
        r = solve_lp(make_lp(**kw))
        if r.status != "infeasible":
            continue
        lp = make_lp(**kw)
        A_le, b_le, A_eq, b_eq, eff_ub = canonical_arrays(lp)
        chk = verify_certificate(
            r.certificate["rows"], A_le, b_le, A_eq, b_eq, eff_ub
        )
        assert chk["valid"]
        assert chk["muTb"] < -TOL.cert
        found += 1
    assert found >= 5


def test_medium_size_runs_fast():
    """接近声明规模上限的问题（50 变量/60 约束）应快速完成。"""
    rng = np.random.default_rng(77)
    n, m = 50, 60
    A = rng.normal(size=(m, n))
    # 让可行域有内点：取 x* 为小正值并放宽右端。
    x_star = rng.uniform(0.2, 1.0, size=n)
    b = A @ x_star + rng.uniform(0.5, 2.0, size=m)
    c = rng.normal(size=n)
    t0 = time.perf_counter()
    r = solve_lp(make_lp(c=c.tolist(), A_ub=A.tolist(), b_ub=b.tolist(),
                         ub=[5.0] * n))
    dt = time.perf_counter() - t0
    assert r.status == "optimal"
    assert dt < 5.0, f"中等规模耗时 {dt:.2f}s 超出预期"
    lp = make_lp(c=c.tolist(), A_ub=A.tolist(), b_ub=b.tolist(), ub=[5.0] * n)
    A_le, b_le, _, _, eff_ub = canonical_arrays(lp)
    res = solution_residuals(r.x - lp.shift, A_le, b_le, None, None, eff_ub)
    assert res["feasible"]
    assert sum(r.iterations) < 2000


def test_near_degenerate_ill_conditioned():
    """近冗余约束（系数相差 1e-6）仍应正确求解。"""
    eps = 1e-6
    r = solve_lp(make_lp(
        c=[-1, -1],
        A_ub=[[1, 1], [1 + eps, 1]], b_ub=[2, 2 + eps],
    ))
    assert r.status == "optimal"
    assert abs(r.objective - (-2)) < 1e-6


def test_integer_small_problems_known_values():
    """一组整数系数问题的已知答案。"""
    cases = [
        # 运输风味小例：min 2x+3y, x+y>=4, x<=3, y<=3 -> x=3,y=1 -> 9
        (dict(c=[2, 3], A_ub=[[-1, -1], [1, 0], [0, 1]],
              b_ub=[-4, 3, 3]), 9),
        # 配料：min 4x+2y, 3x+y>=6, x+y=4 -> x=1,y=3 -> 10
        (dict(c=[4, 2], A_ub=[[-3, -1]], b_ub=[-6],
              A_eq=[[1, 1]], b_eq=[4]), 10),
        # 纯等式：min x+z, x+y+z=6, y=2 -> 4
        (dict(c=[1, 0, 1], A_eq=[[1, 1, 1], [0, 1, 0]],
              b_eq=[6, 2]), 4),
    ]
    for kw, want in cases:
        r = solve_lp(make_lp(**kw))
        assert r.status == "optimal", (kw, r.status, r.detail)
        assert abs(r.objective - want) < 1e-8, (kw, r.objective, want)
