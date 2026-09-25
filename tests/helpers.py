"""测试辅助：构造 LP、在"平移后规范系统"上做独立残差核验。"""

from __future__ import annotations

from blp import make_lp, solve_lp
from blp.model import TOL
from blp.verify import ray_residuals, solution_residuals, verify_certificate


def canonical_arrays(lp):
    """从 LP 取平移后的 A_le/b_le、A_eq/b_eq、ub（规范系统）。"""
    A_le, b_le = (lp.A_ub, lp.b_ub) if lp.A_ub is not None else (None, None)
    A_eq, b_eq = (lp.A_eq, lp.b_eq) if lp.A_eq is not None else (None, None)
    return A_le, b_le, A_eq, b_eq, lp.ub


def check_optimal(c, expect_obj, *, A_ub=None, b_ub=None, A_eq=None,
                  b_eq=None, lb=None, ub=None, sense="min",
                  x_tol=1.0e-7):
    """求解并全面核验最优结果；返回 SolveResult。"""
    lp = make_lp(c=c, sense=sense, A_ub=A_ub, b_ub=b_ub,
                 A_eq=A_eq, b_eq=b_eq, lb=lb, ub=ub)
    r = solve_lp(lp)
    assert r.status == "optimal", (
        f"状态应为 optimal，得到 {r.status}: {r.detail}"
    )
    y = r.x - lp.shift
    A_le, b_le, A_eq, be, eff_ub = canonical_arrays(lp)
    res = solution_residuals(y, A_le, b_le, A_eq, be, eff_ub)
    assert res["feasible"], f"解不可行：{res}"
    assert abs(r.objective - expect_obj) <= x_tol, (
        f"目标值 {r.objective} 与期望 {expect_obj} 不符"
    )
    assert abs(float(lp.c @ r.x) + lp.c0 - expect_obj) <= x_tol
    return r


def check_infeasible(**kw):
    """求解不可行问题并核验 Farkas 证书。"""
    lp = make_lp(**kw)
    r = solve_lp(lp)
    assert r.status == "infeasible", (
        f"状态应为 infeasible，得到 {r.status}: {r.detail}"
    )
    A_le, b_le, A_eq, b_eq, eff_ub = canonical_arrays(lp)
    chk = verify_certificate(
        r.certificate["rows"], A_le, b_le, A_eq, b_eq, eff_ub
    )
    assert chk["valid"], f"证书核验未通过：{chk}"
    return r, chk


def check_unbounded(**kw):
    """求解无界问题并核验射线。"""
    lp = make_lp(**kw)
    r = solve_lp(lp)
    assert r.status == "unbounded", (
        f"状态应为 unbounded，得到 {r.status}: {r.detail}"
    )
    A_le, b_le, A_eq, b_eq, eff_ub = canonical_arrays(lp)
    y0 = r.x - lp.shift
    res = ray_residuals(y0, r.ray, A_le, b_le, A_eq, b_eq, eff_ub)
    assert res["valid"], f"射线核验未通过：{res}"
    if lp.sense == "min":
        assert r.ray_objective_rate < -TOL.reduced
    else:
        assert r.ray_objective_rate > TOL.reduced
    return r, res
