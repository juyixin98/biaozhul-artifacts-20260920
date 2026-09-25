"""核心求解器测试：手工小整数问题、退化、冗余约束、不可行/无界证书核验。"""

import numpy as np
import pytest

from bounded_lp import LPProblem, solve
from bounded_lp.simplex import OPTIMAL, INFEASIBLE, UNBOUNDED, FAILED
from bounded_lp.enumerate_vertices import reference_solve

FEAS = 1e-7


def assert_feasible(problem, x, tol=FEAS):
    """检查 x 对原始问题可行。"""
    x = np.asarray(x)
    assert np.all(x >= problem.lb - tol)
    assert np.all(x[np.isfinite(problem.ub)] <= problem.ub[np.isfinite(problem.ub)] + tol)
    if problem.A_ub.shape[0]:
        assert np.max(problem.A_ub @ x - problem.b_ub) <= tol
    if problem.A_eq.shape[0]:
        assert np.max(np.abs(problem.A_eq @ x - problem.b_eq)) <= tol


def assert_optimal(problem, result, expected_obj=None, tol=FEAS):
    assert result.status == OPTIMAL, result.message
    assert result.objective is not None
    assert_feasible(problem, result.x, tol)
    # 表与直接代入一致
    assert result.residuals["objective_tableau_vs_direct"] <= 1e-8
    if expected_obj is not None:
        assert abs(result.objective - expected_obj) <= 1e-7, (
            f"目标值 {result.objective} != 期望 {expected_obj}")


# --------------------------------------------------------------------------
# 手工小整数问题
# --------------------------------------------------------------------------


def test_classic_two_var():
    # min -3x1-5x2; x1<=4; 2x2<=12; 3x1+2x2<=18 → 顶点 (2,6) 值 -36
    p = LPProblem(c=[-3, -5],
                  A_ub=[[1, 0], [0, 2], [3, 2]], b_ub=[4, 12, 18])
    r = solve(p)
    assert_optimal(p, r, expected_obj=-36.0)
    assert np.allclose(r.x, [2, 6], atol=1e-8)


def test_maximize():
    p = LPProblem(c=[3, 5], sense="max",
                  A_ub=[[1, 0], [0, 2], [3, 2]], b_ub=[4, 12, 18])
    r = solve(p)
    assert_optimal(p, r, expected_obj=36.0)
    assert np.allclose(r.x, [2, 6], atol=1e-8)


def test_equality_needs_phase1():
    # min x1+x2, x1+x2=2 → 整条边最优，值 2
    p = LPProblem(c=[1, 1], A_eq=[[1, 1]], b_eq=[2])
    r = solve(p)
    assert_optimal(p, r, expected_obj=2.0)
    assert r.phase1_iterations >= 0
    assert abs(sum(r.x) - 2) < 1e-8


def test_mixed_equality_and_inequality():
    # 整数运输型小例：min 2x+3y+z, x+y+z=10, x<=4, y>=2(= -y<=-2)
    p = LPProblem(c=[2, 3, 1],
                  A_ub=[[1, 0, 0], [0, -1, 0]], b_ub=[4, -2],
                  A_eq=[[1, 1, 1]], b_eq=[10])
    r = solve(p)
    # 枚举对照
    st, info = reference_solve(p)
    assert st == OPTIMAL
    assert_optimal(p, r)
    assert abs(r.objective - info["objective"]) < 1e-7


def test_negative_rhs_geq():
    # min 2x1+3x2; x1+x2>=5 → (5,0) 值 10
    p = LPProblem(c=[2, 3], A_ub=[[-1, -1]], b_ub=[-5])
    r = solve(p)
    assert_optimal(p, r, expected_obj=10.0)
    assert np.allclose(r.x, [5, 0], atol=1e-8)


def test_variable_upper_bound():
    p = LPProblem(c=[-1], ub=[10])
    r = solve(p)
    assert_optimal(p, r, expected_obj=-10.0)
    assert abs(r.x[0] - 10) < 1e-8


def test_positive_lower_bound_shift():
    # min -x; x>=2, x<=7 → x=7 值 -7（平移 z=x-2）
    p = LPProblem(c=[-1], lb=[2], ub=[7])
    r = solve(p)
    assert_optimal(p, r, expected_obj=-7.0)


def test_bounded_knapsack():
    # 带变量上界的小问题：max 5x+4y, 6x+4y<=24, x<=3, y<=3
    p = LPProblem(c=[5, 4], sense="max",
                  A_ub=[[6, 4]], b_ub=[24], ub=[3, 3])
    r = solve(p)
    st, info = reference_solve(p)
    assert st == OPTIMAL
    assert_optimal(p, r)
    assert abs(r.objective - info["objective"]) < 1e-7


def test_origin_optimal():
    p = LPProblem(c=[1, 2], A_ub=[[1, 1]], b_ub=[1])
    r = solve(p)
    assert_optimal(p, r, expected_obj=0.0)
    assert np.allclose(r.x, [0, 0])


# --------------------------------------------------------------------------
# 不可行与 Farkas 证书
# --------------------------------------------------------------------------


def test_infeasible_equalities():
    p = LPProblem(c=[1, 1], A_eq=[[1, 1], [1, 1]], b_eq=[1, 3])
    r = solve(p)
    assert r.status == INFEASIBLE
    y = np.asarray(r.farkas_y)
    A, b = p.A_eq, p.b_eq
    # y^T A <= 0 且 y^T b > 0（Farkas 证书）
    assert np.max(A.T @ y) <= FEAS
    assert y @ b > FEAS
    assert r.residuals["farkas_violation"] <= FEAS


def test_infeasible_mixed_rows_certificate():
    # 含松弛基行的不可行系统，证书必须仍是非零有效证书
    p = LPProblem(c=[1, 1],
                  A_ub=[[1, 1]], b_ub=[0.5],
                  A_eq=[[1, 1], [1, 1]], b_eq=[1, 3])
    r = solve(p)
    assert r.status == INFEASIBLE
    sf = p.standard_form()
    y = np.asarray(r.farkas_y)
    assert np.max(sf.A[:, :sf.A.shape[1] - sf.n_artificial].T @ y) <= FEAS
    assert y @ sf.b > FEAS


def test_infeasible_negative_bound():
    # x1 >= 1 与 x1 <= 0
    p = LPProblem(c=[1], A_ub=[[1]], b_ub=[0], lb=[1])
    r = solve(p)
    assert r.status == INFEASIBLE


# --------------------------------------------------------------------------
# 无界与射线
# --------------------------------------------------------------------------


def test_unbounded_simple():
    p = LPProblem(c=[-1, -2], A_ub=[[-1, -1]], b_ub=[1])
    r = solve(p)
    assert r.status == UNBOUNDED
    d = r.ray
    assert r.objective_direction < 0
    assert np.min(d) >= -FEAS                       # d >= 0
    assert np.max(p.A_ub @ d) <= FEAS               # A d <= 0
    assert p.c @ d < -FEAS                           # 目标改善
    # x + t d 对任意 t>=0 可行
    x = r.x
    assert_feasible(p, x + 1e6 * d)


def test_unbounded_with_fixed_variable():
    # 一个变量被上界锁死，另一个自由
    p = LPProblem(c=[-1, -1], A_ub=[[1, 0]], b_ub=[5])
    r = solve(p)
    assert r.status == UNBOUNDED
    d = r.ray
    assert abs(d[0]) <= FEAS and abs(d[1] - 1) <= FEAS


def test_unbounded_free_variable():
    assert solve(LPProblem(c=[1], sense="max")).status == UNBOUNDED
    assert solve(LPProblem(c=[-1])).status == UNBOUNDED


def test_unbounded_ray_equality_direction():
    # x1 - x2 = 1, min -x1；射线必须满足 A_eq d = 0
    p = LPProblem(c=[-1, 0], A_eq=[[1, -1]], b_eq=[1])
    r = solve(p)
    assert r.status == UNBOUNDED
    d = r.ray
    assert np.max(np.abs(p.A_eq @ d)) <= FEAS
    assert np.min(d) >= -FEAS


# --------------------------------------------------------------------------
# 退化与冗余约束
# --------------------------------------------------------------------------


def test_degenerate_vertex():
    # 在原点有 3 个活跃约束但只需 2 个（退化）：
    # min -x1-x2; x1+x2<=1; x1<=1; x2<=1（原点退化）
    p = LPProblem(c=[-1, -1],
                  A_ub=[[1, 1], [1, 0], [0, 1]], b_ub=[1, 1, 1])
    r = solve(p)
    assert_optimal(p, r, expected_obj=-1.0)


def test_redundant_constraints():
    # 重复行、成比例行、被包含的行
    p = LPProblem(
        c=[-3, -5],
        A_ub=[[1, 0], [0, 2], [3, 2],     # 原问题
              [1, 0], [2, 0], [0, 4],     # 重复 / 成比例
              [-1, 0], [0, -1]],          # 非负冗余
        b_ub=[4, 12, 18, 4, 8, 24, 0, 0],
    )
    r = solve(p)
    assert_optimal(p, r, expected_obj=-36.0)
    assert np.allclose(r.x, [2, 6], atol=1e-7)


def test_redundant_equality():
    # 两个相同等式 + 一个等式的线性组合
    p = LPProblem(c=[1, -2],
                  A_eq=[[1, 1], [1, 1], [2, 2]], b_eq=[4, 4, 8])
    r = solve(p)
    assert_optimal(p, r, expected_obj=-8.0)   # x=(0,4)


def test_implied_equality_and_inequality():
    # 等式 x+y=4 下 x+y<=4 冗余（Phase I 需处理人工变量逐出）
    p = LPProblem(c=[-1, -1],
                  A_ub=[[1, 1]], b_ub=[4],
                  A_eq=[[1, 1]], b_eq=[4])
    r = solve(p)
    assert_optimal(p, r, expected_obj=-4.0)


# --------------------------------------------------------------------------
# Beale 循环问题：Bland 终止，Dantzig 浮点不挂，精确有理数版确定性循环
# --------------------------------------------------------------------------

BEALE_C = [-3, 3000, -2, 60]            # 乘 4/消分母形式见下，使用经典分数
BEALE_C = [-3 / 4, 150, -1 / 50, 6]
BEALE_A = [
    [1 / 4, -60, -1 / 25, 9],
    [1 / 2, -90, -1 / 50, 3],
    [0, 0, 1, 0],
]
BEALE_B = [0, 0, 1]


def beale_problem():
    eye = np.eye(3)
    A = np.hstack([BEALE_A, eye])
    return LPProblem(c=BEALE_C + [0, 0, 0], A_eq=A.tolist(), b_eq=BEALE_B)


def test_beale_bland_finds_optimum():
    r = solve(beale_problem(), rule="bland")
    assert_optimal(beale_problem(), r, expected_obj=-1 / 20)


def test_beale_dantzig_float_does_not_hang():
    # 浮点下 Dantzig 可能被舍入“解救”；无论最优还是 failed，必须在
    # 迭代上限内终止且不返回错误结论（failed 时不携带解）。
    r = solve(beale_problem(), rule="dantzig", max_iterations=500)
    assert r.status in (OPTIMAL, FAILED)
    if r.status == OPTIMAL:
        assert abs(r.objective - (-0.05)) <= 1e-7
    else:
        assert r.reason in ("iteration_limit", "cycle_detected")
        assert r.x is None


def test_beale_cycles_under_exact_arithmetic():
    """用 fractions.Fraction 的精确表上单纯形确定性地再现 Beale 循环。

    这是不依赖浮点行为的循环存在性证据：同一问题同一规则，精确算术下
    6 次枢轴后基组合回到起点。
    """
    from fractions import Fraction

    def F(x):
        return Fraction(x).limit_denominator(1000)

    c = [F(v) for v in BEALE_C] + [F(0)] * 3
    A = [[F(v) for v in row] + [F(int(i == k)) for k in range(3)]
         for i, row in enumerate(BEALE_A)]
    b = [F(v) for v in BEALE_B]
    m, N = 3, 7
    # 表：行0 目标；人工为这 3 个初始基（这里 slack 列即单位列，无需人工，
    # 直接从给定 BFS 开始 Phase II 风格迭代，经典循环发生在此阶段）
    T = [[F(0)] * (N + 1) for _ in range(m + 1)]
    for i in range(m):
        for j in range(N):
            T[i + 1][j] = A[i][j]
        T[i + 1][N] = b[i]
    for j in range(N):
        T[0][j] = c[j] - sum(c[4 + i] * A[i][j] for i in range(m))
    basis = [4, 5, 6]

    def pivot(row, col):
        pv = T[row][col]
        T[row] = [v / pv for v in T[row]]
        for r in range(m + 1):
            if r != row:
                T[r] = [T[r][j] - T[r][col] * T[row][j] for j in range(N + 1)]

    seen = [tuple(basis)]
    cycled = False
    for _ in range(20):
        # Dantzig：检验数最负入基（列 0..3，slack 列不主动入基也无所谓）
        neg = [j for j in range(N) if T[0][j] < 0]
        if not neg:
            break
        e = min(neg, key=lambda j: T[0][j])
        ratios = []
        for i in range(m):
            if T[i + 1][e] > 0:
                ratios.append((T[i + 1][N] / T[i + 1][e], i))
        r = min(ratios, key=lambda t: t[0])[1] + 1
        pivot(r, e)
        basis[r - 1] = e
        key = tuple(basis)
        if key in seen:
            cycled = True
            break
        seen.append(key)
    assert cycled, "精确算术下 Beale 问题应在有限步内重现基组合（循环）"
