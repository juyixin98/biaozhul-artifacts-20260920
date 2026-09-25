"""随机小整数 LP：单纯形结果与顶点枚举参考实现批量对照。

两种算法完全独立（表上单纯形 vs 组合枚举所有基），
对每个随机问题要求状态一致；最优时目标值一致、双方解互相可行。
"""

import numpy as np
import pytest

from bounded_lp import LPProblem, solve
from bounded_lp.simplex import OPTIMAL, INFEASIBLE, UNBOUNDED
from bounded_lp.enumerate_vertices import reference_solve

rng_seed = 20260923


def make_problem(rng, n, m_ub=0, m_eq=0, with_bounds=False, coef_range=(-3, 4)):
    A_ub = rng.integers(coef_range[0], coef_range[1], size=(m_ub, n)).tolist() if m_ub else None
    b_ub = rng.integers(1, 8, size=m_ub).tolist() if m_ub else None
    A_eq = rng.integers(coef_range[0], coef_range[1], size=(m_eq, n)).tolist() if m_eq else None
    b_eq = rng.integers(0, 6, size=m_eq).tolist() if m_eq else None
    ub = rng.integers(1, 6, size=n).astype(float).tolist() if with_bounds else None
    sense = "min" if rng.random() < 0.7 else "max"
    c = rng.integers(-4, 5, size=n).tolist()
    return LPProblem(c=c, sense=sense,
                     A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, ub=ub)


@pytest.mark.parametrize("seed", range(40))
def test_random_problems_match_enumeration(seed):
    rng = np.random.default_rng(rng_seed + seed)
    n = int(rng.integers(2, 5))
    m_ub = int(rng.integers(0, 5))
    m_eq = int(rng.integers(0, 2))
    with_bounds = bool(rng.random() < 0.4)
    p = make_problem(rng, n, m_ub, m_eq, with_bounds)

    r = solve(p, rule="bland")
    ref_status, ref_info = reference_solve(p)

    assert r.status == ref_status, (
        f"状态不一致 seed={seed}: simplex={r.status}, enum={ref_status}")

    if r.status == OPTIMAL:
        assert abs(r.objective - ref_info["objective"]) < 1e-6
        # 参考解也必须通过求解器残差意义下可行
        x_ref = ref_info["x"]
        if p.A_ub.shape[0]:
            assert np.max(p.A_ub @ x_ref - p.b_ub) <= 1e-6
        if p.A_eq.shape[0]:
            assert np.max(np.abs(p.A_eq @ x_ref - p.b_eq)) <= 1e-6
        assert r.residuals["max_abs_residual"] <= 1e-6
        assert r.residuals["reduced_cost_violation"] <= 1e-7
    elif r.status == UNBOUNDED:
        # 枚举找到射线；求解器射线同样严格改善
        d = r.ray
        cdir = p.c @ d * (1 if p.sense == "min" else -1)
        assert cdir < -1e-7
        if p.A_ub.shape[0]:
            assert np.max(p.A_ub @ d) <= 1e-6
        if p.A_eq.shape[0]:
            assert np.max(np.abs(p.A_eq @ d)) <= 1e-6
        assert np.min(d) >= -1e-7


def test_random_infeasible_certificates_valid():
    """随机制造冲突等式，检查每个 Farkas 证书在原系统上成立。"""
    rng = np.random.default_rng(rng_seed + 100)
    found = 0
    for seed in range(60):
        r = np.random.default_rng(rng_seed + 200 + seed)
        n = int(r.integers(2, 4))
        a = r.integers(-3, 4, size=n)
        if np.all(a == 0):
            continue
        p = LPProblem(c=r.integers(-2, 3, size=n).tolist(),
                      A_eq=np.vstack([a, a]).tolist(),
                      b_eq=[float(r.integers(0, 4)), float(r.integers(5, 9))])
        res = solve(p)
        if res.status != INFEASIBLE:
            continue
        found += 1
        y = np.asarray(res.farkas_y)
        assert np.max(p.A_eq.T @ y) <= 1e-7
        assert y @ p.b_eq > 1e-7
    assert found >= 5, "随机试验中应至少构造出 5 个不可行问题"


def test_optimal_value_matches_at_multiple_vertices():
    """多最优解情形：求解器给的解与枚举最优值相同（不强求同一顶点）。"""
    # min x1+x2, x1+x2=2：整条棱都是最优
    p = LPProblem(c=[1, 1], A_eq=[[1, 1]], b_eq=[2])
    r = solve(p)
    _, info = reference_solve(p)
    assert r.status == OPTIMAL
    assert abs(r.objective - info["objective"]) < 1e-8
