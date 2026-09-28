"""核心数据结构测试：弧分类、状态扩展、出弧索引一致性。"""

from __future__ import annotations

import pytest

from wfst.core.fst import EPSILON, Arc, ArcKind, FST


def test_arc_kind_distinguishes_four_epsilon_cases():
    assert Arc(0, 1, "a", "b").kind() is ArcKind.NORMAL
    assert Arc(0, 1, EPSILON, "b").kind() is ArcKind.EPS_IN
    assert Arc(0, 1, "a", EPSILON).kind() is ArcKind.EPS_OUT
    assert Arc(0, 1, EPSILON, EPSILON).kind() is ArcKind.EPS_EPS


def test_input_and_output_epsilon_are_separate_positions():
    # 同一个字面量 <eps>，因所在侧不同而语义不同：插入 vs 删除。
    insertion = Arc(0, 1, EPSILON, "x")
    deletion = Arc(0, 1, "x", EPSILON)
    assert insertion.kind() is not deletion.kind()
    assert insertion.ilabel == deletion.olabel == EPSILON


def test_add_arc_extends_state_space_implicitly():
    fst = FST()
    fst.add_arc(0, 3, "a", "b")  # 状态 1/2/3 此前未显式创建
    assert fst.num_states == 4
    assert len(fst.outgoing(3)) == 0
    assert fst.outgoing(0)[0].dst == 3


def test_set_final_extends_state_space():
    fst = FST()
    fst.set_final(2, 0.5)
    assert fst.num_states == 3
    assert fst.is_final(2)
    assert fst.final_weight(2) == pytest.approx(0.5)
    assert fst.final_weight(0) > 1e300  # 非终态代价为 +inf


def test_outgoing_index_reflects_all_arcs():
    fst = FST()
    fst.add_arc(0, 1, "a", "a")
    fst.add_arc(0, 1, "b", "b")
    fst.add_arc(1, 0, "c", "c")
    assert len(fst.outgoing(0)) == 2
    assert len(fst.outgoing(1)) == 1
    # 符号表不含 epsilon。
    assert "a" in fst.input_symbols and EPSILON not in fst.input_symbols


def test_arc_is_frozen():
    arc = Arc(0, 1, "a", "b", 1.0)
    with pytest.raises(Exception):
        arc.weight = 2.0  # type: ignore[misc]
