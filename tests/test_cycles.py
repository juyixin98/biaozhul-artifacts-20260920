"""环检测测试：负代价环与纯输入 epsilon 发射环的明确识别范围。"""

from __future__ import annotations

from wfst.core.cycles import (
    negative_cycle_on_accepting_paths,
    reachable_input_epsilon_cycle,
)
from wfst.core.fst import EPSILON, FST


def test_detects_negative_self_loop_on_accepting_path():
    fst = FST("neg")
    fst.add_arc(0, 1, "a", "x", 0.0)
    fst.add_arc(1, 1, EPSILON, EPSILON, -0.5)
    fst.set_final(1)
    cyc = negative_cycle_on_accepting_paths(fst)
    assert cyc is not None
    assert cyc[0] == cyc[-1] == 1


def test_no_negative_cycle_with_negative_arc():
    fst = FST("neg_arc")
    fst.add_arc(0, 1, "a", "x", -1.0)
    fst.add_arc(1, 2, "b", "y", 0.5)
    fst.set_final(2)
    assert negative_cycle_on_accepting_paths(fst) is None


def test_negative_cycle_unreachable_from_start_ignored():
    # 负环位于从初态不可达的状态，不影响接受路径。
    fst = FST("iso")
    fst.add_arc(0, 1, "a", "x", 0.0)
    fst.set_final(1)
    fst.add_arc(2, 3, EPSILON, EPSILON, -0.1)
    fst.add_arc(3, 2, EPSILON, EPSILON, -0.1)
    assert negative_cycle_on_accepting_paths(fst) is None


def test_input_epsilon_emitting_cycle_detected():
    # 不消耗输入、却发射输出的输入 epsilon 环（无穷多输出来源）。
    fst = FST("emit_loop")
    fst.add_arc(0, 0, EPSILON, "z", 0.05)
    fst.add_arc(0, 1, "a", "x", 0.0)
    fst.set_final(1)
    cyc = reachable_input_epsilon_cycle(fst)
    assert cyc is not None
    assert cyc[0] == cyc[-1] == 0


def test_no_input_epsilon_cycle_on_consuming_loop():
    # 需要消耗真实输入的环不是输入 epsilon 环。
    fst = FST("consume_loop")
    fst.add_arc(0, 0, "a", "x", 0.0)
    fst.set_final(0)
    assert reachable_input_epsilon_cycle(fst) is None
