"""经典小整数问题：与手工最优值对照，并检查三种终态。"""

from helpers import check_infeasible, check_optimal, check_unbounded


def test_classic_two_var_factory():
    # 教科书例：min -3x1-5x2，x1<=4, 2x2<=12, 3x1+2x2<=18。
    r = check_optimal(
        [-3, -5], -36,
        A_ub=[[1, 0], [0, 2], [3, 2]], b_ub=[4, 12, 18],
    )
    assert abs(r.x[0] - 2) < 1e-8 and abs(r.x[1] - 6) < 1e-8


def test_maximization():
    r = check_optimal(
        [3, 5], 36, sense="max",
        A_ub=[[1, 0], [0, 2], [3, 2]], b_ub=[4, 12, 18],
    )
    assert abs(r.x[0] - 2) < 1e-8 and abs(r.x[1] - 6) < 1e-8


def test_equality_only_optimal():
    # min x, x+y=1 -> 0
    check_optimal([1, 0], 0, A_eq=[[1, 1]], b_eq=[1])


def test_ge_constraint():
    # min x+y, x+y>=1（取反为 <=）-> 1
    check_optimal([1, 1], 1, A_ub=[[-1, -1]], b_ub=[-1])


def test_mixed_eq_ub():
    # min x+2y, x+y=1, x<=0.4 -> y=0.6, obj=1.6
    check_optimal(
        [1, 2], 1.6,
        A_ub=[[1, 0]], b_ub=[0.4],
        A_eq=[[1, 1]], b_eq=[1],
    )


def test_lower_bound_shift():
    # min x, x>=2 -> 2
    check_optimal([1], 2, lb=[2])


def test_upper_bound_binds():
    # min -x, x<=3 -> -3
    check_optimal([-1], -3, ub=[3])


def test_fixed_variable():
    # lb==ub 固定变量。
    check_optimal([1], 2, lb=[2], ub=[2])


def test_infeasible_equalities():
    check_infeasible(c=[1], A_eq=[[1], [1]], b_eq=[1, 2])


def test_infeasible_bounds():
    check_infeasible(c=[1], lb=[3], ub=[1])


def test_infeasible_ineq_and_eq():
    # x<=1 与 x=2
    check_infeasible(
        c=[1], A_ub=[[1]], b_ub=[1], A_eq=[[1]], b_eq=[2]
    )


def test_unbounded_free_direction():
    check_unbounded(c=[-1])


def test_unbounded_with_constraint():
    # min -2x+y, x-y<=5，射线 (1,1)
    check_unbounded(c=[-2, 1], A_ub=[[1, -1]], b_ub=[5])


def test_unbounded_maximization():
    check_unbounded(c=[1, 1], sense="max")


def test_objective_constant():
    # min x + 7, 无约束（x>=0）-> 7（x=0）。
    from blp import make_lp, solve_lp
    r = solve_lp(make_lp(c=[1], c0=7.0))
    assert r.status == "optimal"
    assert abs(r.objective - 7.0) < 1e-12


def test_objective_constant_with_lower_bound():
    # 常数项不得与变量平移重复计入：min x + 10, x>=2 -> 12。
    from blp import make_lp, solve_lp
    r = solve_lp(make_lp(c=[1], c0=10.0, lb=[2]))
    assert r.status == "optimal"
    assert abs(r.objective - 12.0) < 1e-12


def test_zero_objective_feasible():
    check_optimal([0, 0, 0], 0, A_ub=[[1, 1, 1]], b_ub=[5])
