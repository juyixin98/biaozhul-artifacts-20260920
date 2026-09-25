"""边缘情形与输入校验测试。"""

import numpy as np
import pytest

from bounded_lp import LPProblem, solve
from bounded_lp.errors import InvalidProblem
from bounded_lp.simplex import OPTIMAL, INFEASIBLE, UNBOUNDED


def test_zero_objective():
    r = solve(LPProblem(c=[0, 0], A_ub=[[1, 1]], b_ub=[1]))
    assert r.status == OPTIMAL and r.objective == 0.0


def test_zero_column_is_unbounded():
    # 第一列系数为 0（x1 不出现在约束中），费用 -1：沿 x1 无界
    r = solve(LPProblem(c=[-1, 1], A_ub=[[0, 1]], b_ub=[2]))
    assert r.status == UNBOUNDED
    assert abs(r.ray[0] - 1) <= 1e-8


def test_redundant_zero_equality_leaves_unconstrained():
    # 0*x = 0 是恒真行；无其它约束、费用 -1 → 无界
    r = solve(LPProblem(c=[-1], A_eq=[[0]], b_eq=[0]))
    assert r.status == UNBOUNDED


def test_contradiction_zero_equals_one():
    r = solve(LPProblem(c=[1], A_eq=[[0]], b_eq=[1]))
    assert r.status == INFEASIBLE
    assert r.residuals["farkas_ytb"] > 1e-7
    assert r.residuals["farkas_violation"] <= 1e-7


def test_redundant_dependent_equalities():
    # 第三个等式是前两个之和
    r = solve(LPProblem(c=[-1, -1],
                        A_eq=[[1, 0], [0, 1], [1, 1]], b_eq=[1, 1, 2]))
    assert r.status == OPTIMAL
    assert np.allclose(r.x, [1, 1])
    assert abs(r.objective - -2.0) < 1e-8


def test_zero_rhs_degenerate_origin():
    r = solve(LPProblem(c=[-1, -1],
                        A_ub=[[1, 0], [0, 1], [1, 1]], b_ub=[0, 0, 0]))
    assert r.status == OPTIMAL and abs(r.objective) < 1e-10


def test_shift_with_equality():
    r = solve(LPProblem(c=[-1], lb=[2], A_eq=[[1]], b_eq=[5]))
    assert r.status == OPTIMAL and abs(r.x[0] - 5) < 1e-8
    assert abs(r.objective - -5.0) < 1e-8


def test_lower_bound_conflicts_equality():
    r = solve(LPProblem(c=[1], lb=[3], A_eq=[[1]], b_eq=[1]))
    assert r.status == INFEASIBLE


@pytest.mark.parametrize("kwargs", [
    dict(c=[1] * 301),
    dict(c=[1], lb=[-1e-8 - 1.0]),
])
def test_size_and_nonneg_validation(kwargs):
    with pytest.raises(InvalidProblem):
        LPProblem(**kwargs)


def test_coefficient_magnitude_limit():
    with pytest.raises(InvalidProblem):
        LPProblem(c=[1e7])


def test_dimension_mismatch_rejected():
    with pytest.raises(InvalidProblem):
        LPProblem(c=[1, 2], A_ub=[[1, 0, 0]], b_ub=[1])
    with pytest.raises(InvalidProblem):
        LPProblem(c=[1, 2], A_ub=[[1, 0]], b_ub=[1, 2])


def test_nan_rejected():
    with pytest.raises(InvalidProblem):
        LPProblem(c=[float("nan"), 1])


def test_invalid_sense_rejected():
    with pytest.raises(InvalidProblem):
        LPProblem(c=[1], sense="maximum")


def test_solver_rejects_unknown_rule():
    p = LPProblem(c=[1])
    with pytest.raises(InvalidProblem):
        solve(p, rule="steepest")
