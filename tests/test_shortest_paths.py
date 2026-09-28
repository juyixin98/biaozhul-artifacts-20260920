"""n-最短枚举测试：排序、负权支持范围、负环、预算、epsilon 环、拒绝类别。"""

from __future__ import annotations

import pytest

from wfst.algorithms.shortest_paths import nbest_paths
from wfst.builders import chain_acceptor
from wfst.core.fst import (
    EPSILON,
    AlignmentError,
    FST,
    NegativeCycleError,
)


def test_orders_by_cost_then_lexicographic():
    # 同一输入 a 的三个输出，代价 0.5 / 0.1 / 0.1（后两者平手按字典序）。
    t = FST("amb")
    t.add_arc(0, 1, "a", "c", 0.5)
    t.add_arc(0, 2, "a", "b", 0.1)
    t.add_arc(0, 3, "a", "a", 0.1)
    t.set_final(1)
    t.set_final(2)
    t.set_final(3)
    chained = compose_with(t, "a")
    res = nbest_paths(chained, k=3)
    assert [(h.output, round(h.cost, 3)) for h in res.hypotheses] == [
        ("a", 0.1),
        ("b", 0.1),
        ("c", 0.5),
    ]
    assert res.complete is True


def compose_with(fst: FST, text: str) -> FST:
    from wfst.algorithms.compose import compose

    return compose(chain_acceptor(list(text)), fst)


def test_distinct_outputs_dedup_keep_min_cost():
    # 两条对齐都产出 "x"，只保留最小代价，且不产生重复条目。
    t = FST("dup")
    t.add_arc(0, 1, "a", "x", 0.3)
    t.add_arc(0, 2, EPSILON, "x", 0.0)  # 无输入插入路径
    t.add_arc(2, 1, "a", EPSILON, 0.9)  # 绕路更贵
    t.set_final(1)
    chained = compose_with(t, "a")
    res = nbest_paths(chained, k=10)
    xs = [h for h in res.hypotheses if h.output == "x"]
    assert len(xs) == 1
    assert xs[0].cost == pytest.approx(0.3)


def test_negative_arc_without_negative_cycle_uses_correct_cost():
    # 一条负权 epsilon 弧（非环）：势函数重标度后仍报告原始总代价。
    t = FST("neg_arc")
    t.add_arc(0, 1, "a", "X", 1.0)
    t.add_arc(1, 2, EPSILON, "Y", -0.4)
    t.set_final(2)
    res = nbest_paths(compose_with(t, "a"), k=3)
    assert [(h.output, round(h.cost, 3)) for h in res.hypotheses] == [("XY", 0.6)]


def test_negative_cycle_raises_not_success():
    t = FST("neg_cycle")
    t.add_arc(0, 1, "a", "X", 0.0)
    t.add_arc(1, 1, EPSILON, EPSILON, -0.1)  # 负代价 ε:ε 自环
    t.set_final(1)
    with pytest.raises(NegativeCycleError) as exc:
        nbest_paths(compose_with(t, "a"), k=3)
    cyc = exc.value.cycle_states
    assert cyc  # 报告环上状态，明确失败依据
    assert cyc[0] == cyc[-1]  # 还原出的是一个闭合环


def test_positive_epsilon_self_loop_terminates_and_budget_marks_incomplete():
    # 正代价插入自环：可产生无穷输出，预算是终止保障。
    t = FST("inf_out")
    t.add_arc(0, 0, "a", "X", 0.0)
    t.add_arc(0, 0, EPSILON, "Z", 0.05)
    t.set_final(0)
    res_full = nbest_paths(compose_with(t, "a"), k=5, budget=100_000)
    assert [h.output for h in res_full.hypotheses[:3]] == ["X", "XZ", "ZX"]
    assert res_full.complete is True  # 找满 k 条即完备

    res_cut = nbest_paths(compose_with(t, "a"), k=1000, budget=30)
    assert res_cut.complete is False  # 预算耗尽，明确标记未完成
    assert 0 < len(res_cut.hypotheses) < 1000
    assert res_cut.pops >= 30


def test_zero_cost_epsilon_loop_does_not_hang():
    # 零代价 ε:ε 自环：优势去重保证只展开一次，正常终止。
    t = FST("zero_loop")
    t.add_arc(0, 0, "a", "X", 0.0)
    t.add_arc(0, 0, EPSILON, EPSILON, 0.0)
    t.set_final(0)
    res = nbest_paths(compose_with(t, "a"), k=3, budget=1000)
    assert [h.output for h in res.hypotheses] == ["X"]
    assert res.complete is True


def test_unacceptable_input_raises_alignment_error():
    t = FST("only_b")
    t.add_arc(0, 1, "b", "Y", 0.0)
    t.set_final(1)
    with pytest.raises(AlignmentError):
        nbest_paths(compose_with(t, "a"), k=3)


def test_invalid_k_raises():
    t = FST("t")
    t.set_final(0)
    with pytest.raises(ValueError):
        nbest_paths(t, k=0)


def test_final_weight_is_part_of_path_cost():
    # 终止权重不同：输出相同串，取较小终止权重。
    t = FST("final_w")
    t.add_arc(0, 1, "a", "X", 0.0)
    t.add_arc(0, 2, "a", "X", 0.0)
    t.set_final(1, 0.2)
    t.set_final(2, 0.9)
    res = nbest_paths(compose_with(t, "a"), k=5)
    assert [(h.output, round(h.cost, 3)) for h in res.hypotheses] == [("X", 0.2)]
