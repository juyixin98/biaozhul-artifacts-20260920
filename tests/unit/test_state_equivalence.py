"""状态等价判据单元测试（契约 1）。

关键：状态等价必须**同时**比较终结标记与转移。这里用构造出来的具体状态
断言哪些键相等、哪些不等，尤其覆盖"子节点数量相同但不应合并"的反例。
"""

from __future__ import annotations

import pytest

from app.core.state import State, state_key

pytestmark = pytest.mark.unit


def test_state_is_immutable() -> None:
    state = State(0, False, {"a": 1})
    # frozen dataclass 抛 FrozenInstanceError（AttributeError 子类）。
    with pytest.raises(AttributeError):
        state.final = True  # type: ignore[misc]
    with pytest.raises(TypeError):
        state.transitions["b"] = 2  # 只读映射


def test_equivalent_states_have_equal_keys() -> None:
    # 终结标记相同、(符号,目标) 转移集合相同 => 等价（id 无关）。
    left = State(7, True, {"a": 2, "b": 3})
    right = State(9, True, {"b": 3, "a": 2})  # 插入顺序不同
    assert left.key() == right.key()
    assert left.key() == state_key(True, {"a": 2, "b": 3})


def test_same_out_degree_but_different_final_is_not_equivalent() -> None:
    # 反例：子节点数量相同，但终结标记不同 => 必须区分。
    non_terminal = State(1, False, {"a": 2})
    terminal = State(2, True, {"a": 2})
    assert non_terminal.out_degree == terminal.out_degree == 1
    assert non_terminal.key() != terminal.key()


def test_same_out_degree_but_different_symbol_is_not_equivalent() -> None:
    # 反例：子节点数量相同，但出边符号不同 => 必须区分。
    on_a = State(1, False, {"a": 2})
    on_b = State(2, False, {"b": 2})
    assert on_a.out_degree == on_b.out_degree == 1
    assert on_a.key() != on_b.key()


def test_same_symbols_but_different_target_is_not_equivalent() -> None:
    # 反例：符号相同但目标状态不同 => 必须区分。
    to_two = State(1, False, {"a": 2})
    to_three = State(2, False, {"a": 3})
    assert to_two.key() != to_three.key()


def test_final_flag_participates_even_with_no_edges() -> None:
    # 两个叶子：一个终结、一个非终结，绝不能因为"都是 0 个孩子"而合并。
    assert State(1, True, {}).key() != State(2, False, {}).key()
    assert State(1, True, {}).key() == State(3, True, {}).key()
