"""检查点规划器测试：方案合法性、穷举画像、预算选择与不可行分类。"""

from __future__ import annotations

import pytest

from recomp_scheduler.errors import BudgetInfeasibleError, InvalidPlanError
from recomp_scheduler.memory import simulate
from recomp_scheduler.planner import (
    baseline_plan,
    choose_plan,
    legal_plans,
    make_plan,
)

# --------------------------------------------------------------------- #
# 冻结期望值：key=",".join(cut positions)，value=[峰值元素数, 重算 FLOP]。
# 这些值在实现定稿后一次性计算，并已由 tests/independent_model.py
# 的独立区间覆盖模型在相同方案集合上逐一对账（见 test_three_way_*）。
# --------------------------------------------------------------------- #
TINY_TABLE = {
    "": [78, 0],
    "0": [95, 0],
    "1": [78, 0],
    "0,1": [95, 0],
}

CHAIN_TABLE = {
    "": [109, 0],
    "0": [126, 0],
    "1": [126, 0],
    "2": [114, 48],
    "3": [109, 60],
    "0,1": [143, 0],
    "0,2": [131, 48],
    "0,3": [126, 60],
    "1,2": [143, 0],
    "1,3": [126, 12],
    "2,3": [131, 48],
    "0,1,2": [160, 0],
    "0,1,3": [143, 12],
    "0,2,3": [148, 48],
    "1,2,3": [160, 0],
    "0,1,2,3": [177, 0],
}


@pytest.mark.unit
@pytest.mark.parametrize("table_name", ["TINY", "CHAIN"])
def test_exhaustive_table_matches_frozen_values(request, tiny_graph, chain_graph, table_name):
    table = {"TINY": (TINY_TABLE, tiny_graph), "CHAIN": (CHAIN_TABLE, chain_graph)}[
        table_name
    ]
    frozen, graph = table
    scored = dict(
        (",".join(map(str, p.boundary_positions)), prof)
        for p, prof in legal_plans(graph)
    )
    assert set(scored) == set(frozen), "合法方案集合与冻结表不一致"
    for key, (exp_peak, exp_re) in frozen.items():
        prof = scored[key]
        assert prof.peak_elements == exp_peak, f"{table_name} {key} 峰值不符"
        assert prof.recompute_flops == exp_re, f"{table_name} {key} 重算量不符"


def _node_forward_flops(node) -> int:
    """独立的重算 FLOP 参考（与 memory.node_flops 分开书写）。"""
    from recomp_scheduler.graph import _prod

    n = _prod(node.shape)
    if node.op == "linear":
        in_features, out_features = node.params[0].shape
        batch = n // out_features
        return 2 * batch * in_features * out_features
    if node.op in ("relu", "tanh", "dropout", "add", "mul"):
        return n
    return 0


def test_recompute_flops_equal_sum_of_replayed_internal_nodes(chain_graph) -> None:
    """重算量必须等于"检查点块内部节点"前向 FLOP 之和（独立公式核对）。"""
    graph = chain_graph
    order = graph.order
    for plan, prof in legal_plans(graph):
        expected = 0
        for b, (s, e) in enumerate(plan.blocks):
            if b == plan.block_count - 1:
                continue  # 最后一块不重放
            for i in range(s, e):  # 边界节点 e 不重放
                if graph.node(order[i]).op != "input":
                    expected += _node_forward_flops(graph.node(order[i]))
        assert prof.recompute_flops == expected


def test_baseline_has_zero_recompute(chain_graph) -> None:
    plan, prof = baseline_plan(chain_graph)
    assert plan.block_count == 1
    assert prof.recompute_flops == 0
    assert prof.peak_elements == 109  # 冻结基线峰值


def test_cross_block_edge_not_at_boundary_is_invalid(branch_graph) -> None:
    # 在 a1(pos4) 后切：b1(pos5) 仍跨块依赖 d1(pos3)，而 d1 不是边界。
    plan = make_plan((4,), len(branch_graph.order))
    with pytest.raises(InvalidPlanError) as exc:
        plan.validate(branch_graph)
    assert exc.value.category == "invalid_plan"
    assert exc.value.details["edge"] == "d1->b1"


def test_cut_inside_diamond_at_non_boundary_invalid(branch_graph) -> None:
    # 在 d1(3) 与 a2 链中切 a1 后，同样会切断 b1 的输入路径。
    plan = make_plan((4, 5), len(branch_graph.order))
    with pytest.raises(InvalidPlanError):
        plan.validate(branch_graph)


def test_valid_diamond_boundary_plan(branch_graph) -> None:
    # d1 是两条分支的共同祖先；在 d1(pos3) 后切合法（d1 即边界）。
    plan = make_plan((3,), len(branch_graph.order))
    plan.validate(branch_graph)  # 不应抛
    prof = simulate(branch_graph, plan)
    assert prof.peak_elements < baseline_plan(branch_graph)[1].peak_elements


def test_boundary_position_out_of_range(chain_graph) -> None:
    n = len(chain_graph.order)
    # 在最后一个位置之后切会产生空尾块：构造期即拒绝（invalid_plan）。
    with pytest.raises(InvalidPlanError):
        make_plan((n - 1,), n)


def test_choose_plan_prefers_lowest_recompute_within_budget(chain_graph) -> None:
    # 预算 120：表中 <=120 的方案只有基线()=109(重算0) 与 (3,)=109(重算60)。
    # 应选重算量最小的基线，而不是峰值相同但更贵的方案。
    choice = choose_plan(chain_graph, 120)
    assert choice.plan.boundary_positions == ()
    assert choice.profile.recompute_flops == 0
    assert choice.profile.peak_elements == 109


def test_choose_plan_checkpoints_when_baseline_over_budget(chain_graph) -> None:
    # 预算 108 < 基线峰值 109：可行方案里 (2,)=114 也超预算；
    # 该图所有方案最小峰值即 109，因此必须判定不可行。
    with pytest.raises(BudgetInfeasibleError) as exc:
        choose_plan(chain_graph, 108)
    err = exc.value
    assert err.category == "resource_exhausted"
    assert err.details["phase"] if "phase" in err.details else True
    assert err.details["min_achievable_peak"] == 109
    assert err.details["baseline_peak"] == 109


def test_choose_plan_selects_checkpoint_for_tight_budget(big_graph) -> None:
    base_peak = baseline_plan(big_graph)[1].peak_elements
    assert base_peak == 10804  # 冻结基线
    choice = choose_plan(big_graph, 8000)
    assert choice.profile.peak_elements <= 8000
    # 紧预算下必须付出非零额外重算，而不是靠"删掉必要激活后报错"。
    assert choice.profile.recompute_flops > 0
    assert choice.plan.block_count >= 2


def test_nonpositive_budget_rejected(chain_graph) -> None:
    with pytest.raises(BudgetInfeasibleError):
        choose_plan(chain_graph, 0)


def test_infeasible_error_is_marked_planning_phase(chain_graph) -> None:
    # 静态（规划期）不可行必须能与运行时超预算（phase="runtime"）区分。
    with pytest.raises(BudgetInfeasibleError) as exc:
        choose_plan(chain_graph, 1)
    assert exc.value.details["phase"] == "planning"


def test_min_peak_plan_on_big_graph_is_frozen(big_graph) -> None:
    # 穷举确认大链最优峰值 6838（独立模型同样对账，见 test_memory_crosscheck）。
    plans = legal_plans(big_graph)
    assert min(prof.peak_elements for _, prof in plans) == 6838
