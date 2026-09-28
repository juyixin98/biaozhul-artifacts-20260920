"""组合与 epsilon 过滤器测试。

关键性质：每条接受对齐恰有一条路径（无重复计数）；删除后接插入的
L*R* 块既完备又唯一。
"""

from __future__ import annotations

from wfst.algorithms.compose import compose
from wfst.algorithms.epsilon_filter import (
    FilterState,
    MoveKind,
    allowed,
    move_kind_left,
    move_kind_right,
)
from wfst.algorithms.shortest_paths import nbest_paths
from wfst.builders import chain_acceptor
from wfst.core.fst import EPSILON, ArcKind, FST


def _count_paths_to_finals(fst: FST) -> int:
    """用 DFS 统计到终态的路径条数（在无环或用 (状态,输入位置) 去重的
    组合产物上调用）。这里直接按图的走法计数，含自环时仅用于无环用例。"""
    out = fst.outgoing
    memo: dict[int, int] = {}

    def go(s: int) -> int:
        if s in memo:
            return memo[s]
        total = 1 if s in fst.finals else 0
        for arc in out(s):
            if arc.dst != s:  # 测试用例组合产物中忽略自环计数
                total += go(arc.dst)
        memo[s] = total
        return total

    return go(fst.start)


def test_filter_blocks_left_after_right_run():
    # R 段之后禁止 L（防止 L/R 交错重复计数）。
    assert allowed(FilterState.NEUTRAL, MoveKind.LEFT_EPS)
    assert allowed(FilterState.NEUTRAL, MoveKind.RIGHT_EPS)
    assert allowed(FilterState.LEFT_RUN, MoveKind.LEFT_EPS)
    assert allowed(FilterState.LEFT_RUN, MoveKind.RIGHT_EPS)  # L*R*：允许切入 R
    assert not allowed(FilterState.RIGHT_RUN, MoveKind.LEFT_EPS)
    assert allowed(FilterState.RIGHT_RUN, MoveKind.RIGHT_EPS)
    # 真实匹配在任何状态都允许并重置。
    assert allowed(FilterState.RIGHT_RUN, MoveKind.MATCH)


def test_move_kind_uses_correct_side():
    # L 走法看 M1 的输出侧（olabel==ε 即 EPS_OUT/EPS_EPS）；
    # R 走法看 M2 的输入侧（ilabel==ε 即 EPS_IN/EPS_EPS）。
    assert move_kind_left(ArcKind.EPS_OUT) is MoveKind.LEFT_EPS
    assert move_kind_left(ArcKind.EPS_EPS) is MoveKind.LEFT_EPS
    assert move_kind_left(ArcKind.EPS_IN) is MoveKind.MATCH
    assert move_kind_right(ArcKind.EPS_IN) is MoveKind.RIGHT_EPS
    assert move_kind_right(ArcKind.EPS_EPS) is MoveKind.RIGHT_EPS
    assert move_kind_right(ArcKind.EPS_OUT) is MoveKind.MATCH


def test_deletion_then_insertion_block_is_complete():
    # a:ε（删除）接 ε:b（插入）：输入 a 应能产出 b（L 后再 R，过滤器放行）。
    deleter = FST("del")
    deleter.add_arc(0, 0, "a", EPSILON, 0.2)
    deleter.set_final(0)
    inserter = FST("ins")
    inserter.add_arc(0, 0, EPSILON, "b", 0.3)
    inserter.set_final(0)

    block = compose(deleter, inserter)
    chained = compose(chain_acceptor(["a"]), block)
    res = nbest_paths(chained, k=10)
    outputs = {h.output for h in res.hypotheses}
    # 删除 a 得 ""，删除后插入得 "b"，仅插入得 "ba"... 等多类；核心是 "b" 必须存在。
    assert "b" in outputs


def test_no_duplicate_path_count_for_single_alignment():
    # 单个 ε 块若不过滤会有 L、R 两种顺序到达同一乘积状态；过滤后 "b" 仅一条。
    deleter = FST("del")
    deleter.add_arc(0, 0, "a", EPSILON, 0.2)
    deleter.set_final(0)
    inserter = FST("ins")
    inserter.add_arc(0, 0, EPSILON, "b", 0.3)
    inserter.set_final(0)
    block = compose(deleter, inserter)
    chained = compose(chain_acceptor(["a"]), block)
    res = nbest_paths(chained, k=50)
    # 恰好一次插入的输出 "b" 只能有一个代价（0.2+0.3），不出现重复条目。
    b_costs = [round(h.cost, 9) for h in res.hypotheses if h.output == "b"]
    assert b_costs == [0.5]


def test_composition_weights_add_and_parallel_arcs_take_min():
    left = FST("L")
    left.add_arc(0, 1, "a", "x", 0.1)
    left.add_arc(0, 1, "a", "x", 0.4)  # 同标号并行弧
    left.set_final(1)
    right = FST("R")
    right.add_arc(0, 1, "x", "z", 0.2)
    right.set_final(1)
    composed = compose(left, right)
    chained = compose(chain_acceptor(["a"]), composed)
    res = nbest_paths(chained, k=5)
    assert [(h.output, round(h.cost, 6)) for h in res.hypotheses] == [("z", 0.3)]


def test_composition_relational_order():
    # L: a->x ; R: x->y，组合后 a->y。
    left = FST("L")
    left.add_arc(0, 1, "a", "x", 0.0)
    left.set_final(1)
    right = FST("R")
    right.add_arc(0, 1, "x", "y", 0.0)
    right.set_final(1)
    chained = compose(chain_acceptor(["a"]), compose(left, right))
    assert [h.output for h in nbest_paths(chained).hypotheses] == ["y"]
