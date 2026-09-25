"""退化、冗余约束与循环风险的专项测试。

退化：多个约束在同一顶点相交，最小比值为 0，单纯形出现零步长枢轴。
冗余：重复 / 线性相关约束必须被识别为可行（可能删除零行）。
循环：经典 Beale 问题在 Dantzig 规则下退化转圈；本求解器默认 Bland
必须有限终止，Dantzig 规则检测到基重复后自动回退 Bland。
"""

import numpy as np

from blp import make_lp, solve_lp

from helpers import check_infeasible, check_optimal


# --------------------------------------------------------------------------
# 退化
# --------------------------------------------------------------------------


def test_degenerate_vertex_zero_step():
    # 三个约束在 (0,0) 之外的同一退化点相交；最优值在零步长枢轴中保持。
    # min -x-y s.t. x<=1, y<=1, x+y<=2, x+y<=2（重复行制造退化）。
    r = check_optimal(
        [-1, -1], -2,
        A_ub=[[1, 0], [0, 1], [1, 1], [1, 1]],
        b_ub=[1, 1, 2, 2],
    )
    assert abs(r.x[0] - 1) < 1e-8 and abs(r.x[1] - 1) < 1e-8


def test_highly_degenerate_optimal_at_origin():
    # 大量过原点的约束，原点高度退化。
    A = [[1, 0], [0, 1], [1, 1], [2, 1], [1, 2]]
    r = check_optimal([1, 1], 0, A_ub=A, b_ub=[0] * len(A))
    assert abs(r.x[0]) < 1e-10 and abs(r.x[1]) < 1e-10


def test_degenerate_three_d():
    # 3 维退化：4 个有效界面交于一个点。
    r = check_optimal(
        [-1, -1, -1], -1,
        A_ub=[
            [1, 0, 0], [0, 1, 0], [0, 0, 1],
            [1, 1, 1], [1, 1, 1],
        ],
        b_ub=[1, 1, 1, 1, 1],
    )
    assert abs(r.objective - (-1)) < 1e-8


# --------------------------------------------------------------------------
# 冗余约束
# --------------------------------------------------------------------------


def test_duplicated_constraints():
    r = check_optimal(
        [-1, -1], -2,
        A_ub=[[1, 1], [1, 1], [1, 1]], b_ub=[2, 2, 2],
    )
    # 重复约束不应影响最优值。
    assert r.status == "optimal"


def test_implied_redundant_constraint():
    # x+y<=1 已蕴含 2x+2y<=2（方向相同但尺度不同）。
    check_optimal(
        [-1, -1], -1,
        A_ub=[[1, 1], [2, 2]], b_ub=[1, 2],
    )


def test_redundant_equality():
    # 2x+2y=2 是 x+y=1 的冗余等式。
    check_optimal(
        [-1, 0], -1,
        A_eq=[[1, 1], [2, 2]], b_eq=[1, 2],
    )


def test_inconsistent_duplicate_equalities():
    # x=1 与 2x=3 冗余但矛盾 -> 不可行。
    check_infeasible(c=[1], A_eq=[[1], [2]], b_eq=[1, 3])


def test_zero_row_constraint():
    # 0=0 是恒真冗余等式；0=1 则不可行。
    r = solve_lp(make_lp(c=[1], A_eq=[[0]], b_eq=[0]))
    assert r.status == "optimal"
    assert abs(r.objective) < 1e-10
    check_infeasible(c=[1], A_eq=[[0]], b_eq=[1])


# --------------------------------------------------------------------------
# 循环风险：Beale (1955) 问题
# --------------------------------------------------------------------------


def _beale_lp():
    """经典 Beale 循环例：4 个结构变量，3 条 b>=0 的 <= 约束。

    原始 Dantzig 规则（最大检验数、最小行号破并列）在该问题上 6 次
    枢轴后回到初始基（纯零步长循环）。
    """
    return make_lp(
        c=[-3.0 / 4, 150, -1.0 / 50, 6],
        A_ub=[
            [1.0 / 4, -60, -1.0 / 25, 9],
            [1.0 / 2, -90, -1.0 / 50, 3],
            [0, 0, 1, 0],
        ],
        b_ub=[0, 0, 1],
    )


def test_beale_bland_terminates():
    r = solve_lp(_beale_lp(), pivot_rule="bland")
    assert r.status == "optimal"
    assert abs(r.objective - (-0.05)) < 1e-7


def test_beale_dantzig_cycles_then_falls_back_to_bland():
    # Dantzig 在第 6 步回到初始基 => 触发循环检测 => 自动 Bland 重试。
    r = solve_lp(_beale_lp(), pivot_rule="dantzig")
    assert r.status == "optimal", (r.status, r.detail)
    assert abs(r.objective - (-0.05)) < 1e-7
    assert r.pivot_rule == "bland"
    assert any("基重复" in w for w in r.warnings)


def test_low_level_cycle_detection():
    # 直接验证 run_phase 的基重复检测（在 Bland 回退之前拦截）。
    from blp.model import build_standard_form
    from blp.simplex import build_phase1_tableau, run_phase

    sf = build_standard_form(_beale_lp())
    T, basis = build_phase1_tableau(sf)
    # 无人工列时初始目标行为零；填入第二阶段检验数 d = -c。
    T[-1, : sf.n_orig] = -sf.cbar[: sf.n_orig]
    p = run_phase(
        T, basis, np.arange(sf.Abar.shape[1]), rule="dantzig"
    )
    assert p.status == "limit"
    assert p.repeated_basis is True
    assert p.iterations == 6  # 经典循环长度


def test_bland_never_repeats_basis_random_degenerate():
    # 随机退化问题（多个零右端）下 Bland 不应报告任何重复基。
    rng = np.random.default_rng(7)
    for _ in range(20):
        n, m = 4, 5
        A = rng.normal(size=(m, n))
        b = np.zeros(m)  # 全部约束过原点 => 起点高度退化
        c = rng.normal(size=n)
        r = solve_lp(
            make_lp(c=c, A_ub=A.tolist(), b_ub=b.tolist()),
            pivot_rule="bland",
        )
        assert r.status in ("optimal", "unbounded")
        assert not any("重复基" in w for w in r.warnings)
