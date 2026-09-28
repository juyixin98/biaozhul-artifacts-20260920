"""最小性验证：手工推导的最小状态数（非由被测核心生成）。

每个用例的手工推导见注释。若等价签名只按子节点数量合并（契约 1 的反例），
test_transitions_matter 与 test_final_flag_matter 会得到更小的状态数而失败。
"""

import pytest

from minidfa import build_minimal_dfa


@pytest.mark.parametrize(
    ("words", "expected_states"),
    [
        ([], 1),            # 空语言：仅非终结初始状态
        ([""], 1),          # 仅空词：初始状态即终结
        (["a", "b"], 2),    # start + 共享终结态 F
        (["aa", "ab", "ba", "bb"], 3),  # start + 共享中间态 M(a,b->F) + F
        (["bat", "cat", "rat"], 4),     # start + 共享 S(a->M) + 共享 M(t->F) + F
    ],
    ids=["empty", "empty-word", "two-words", "two-level-shared", "shared-suffix"],
)
def test_minimality_hand_computed(words, expected_states):
    dfa = build_minimal_dfa(words)
    assert dfa.state_count == expected_states
    assert sorted(dfa.iter_words()) == sorted(words)


def test_transitions_matter():
    """子节点数相同但转移符号不同，不得合并。

    {ac, bd}: 状态 after-a={c:F} 与 after-b={d:F} 都只有 1 个子节点；
    按子节点数量合并会得到 3 态，正确最小自动机为 4 态。
    """
    dfa = build_minimal_dfa(["ac", "bd"])
    assert dfa.state_count == 4
    assert dfa.contains("ac") and dfa.contains("bd")
    assert not dfa.contains("ad") and not dfa.contains("bc")


def test_final_flag_matter():
    """转移完全相同但终结标记不同，不得合并。

    {ab, abc, gc}: 状态 after-ab=(final,{c:F}) 与 after-g=(non-final,{c:F})
    转移完全一致，仅终结标记不同；忽略终结标记合并会得到 4 态且错误接受 "g"，
    正确最小自动机为 5 态。
    """
    dfa = build_minimal_dfa(["ab", "abc", "gc"])
    assert dfa.state_count == 5
    assert dfa.contains("ab") and dfa.contains("abc") and dfa.contains("gc")
    assert not dfa.contains("g")
    assert not dfa.contains("abcc")
