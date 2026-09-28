"""挖掘内核：从有序词表增量构建最小无环确定自动机。

算法：Daciuk–Mihov–Watson–Watson 增量最小化。每读入一个词，
把前一个词中不再会被后续输入触及的后缀自底向上注册（replace-or-register）。

状态等价签名（契约 1）：同时包含
  - 终结标记 is_final
  - 全部转移 (symbol, 子状态规范身份) 的排序元组
二者共同决定等价类；绝不按子节点数量合并。

前置条件（契约 2）：输入必须字典序升序且无重复 —— 由 corpus 层保证，
本层不信任外部直接调用，入口再做一次断言式校验。
"""

from __future__ import annotations

from typing import Iterable

from .automaton import MinimalDFA
from .corpus import validate_sorted_unique


class _State:
    """构建期的可变状态。冻结后不再使用。"""

    __slots__ = ("is_final", "transitions")

    def __init__(self) -> None:
        self.is_final = False
        self.transitions: dict[str, _State] = {}

    def signature(self) -> tuple[bool, tuple[tuple[str, int], ...]]:
        """等价签名：终结标记 + 按符号排序的 (符号, 子状态身份) 序列。

        子状态必须已完成注册（规范身份稳定），自底向上的处理顺序保证这一点。
        """
        return (
            self.is_final,
            tuple(sorted((sym, id(child)) for sym, child in self.transitions.items())),
        )


def _replace_or_register(
    register: dict[tuple[bool, tuple[tuple[str, int], ...]], _State],
    unchecked: list[tuple[_State, str, _State]],
    down_to: int,
) -> None:
    """把 unchecked 中深度 > down_to 的后缀状态注册为规范状态。"""
    while len(unchecked) > down_to:
        parent, symbol, child = unchecked.pop()
        sig = child.signature()
        canonical = register.get(sig)
        if canonical is None:
            register[sig] = child
        else:
            parent.transitions[symbol] = canonical


def build_minimal_dfa(sorted_words: Iterable[str]) -> MinimalDFA:
    """从有序无重复词表构建最小 DFA。返回冻结的不可变自动机。"""
    words = validate_sorted_unique(sorted_words)

    register: dict[tuple[bool, tuple[tuple[str, int], ...]], _State] = {}
    root = _State()
    unchecked: list[tuple[_State, str, _State]] = []
    previous = ""

    for word in words:
        # 与前一个词的最长公共前缀长度
        common = 0
        limit = min(len(word), len(previous))
        while common < limit and word[common] == previous[common]:
            common += 1

        # 公共前缀之后的后缀已不可能再被后续词触及，注册之
        _replace_or_register(register, unchecked, common)

        anchor = root if common == 0 else unchecked[common - 1][2]
        for ch in word[common:]:
            nxt = _State()
            anchor.transitions[ch] = nxt
            unchecked.append((anchor, ch, nxt))
            anchor = nxt
        anchor.is_final = True
        previous = word

    _replace_or_register(register, unchecked, 0)
    return _freeze(root)


def _freeze(root: _State) -> MinimalDFA:
    """BFS 为可达状态分配整数 id（初始状态为 0），产出不可变结构。"""
    ids: dict[int, int] = {id(root): 0}
    queue: list[_State] = [root]
    finals: set[int] = set()
    transitions: dict[int, tuple[tuple[str, int], ...]] = {}

    while queue:
        state = queue.pop(0)
        src = ids[id(state)]
        if state.is_final:
            finals.add(src)
        edges: list[tuple[str, int]] = []
        for symbol in sorted(state.transitions):
            child = state.transitions[symbol]
            key = id(child)
            if key not in ids:
                ids[key] = len(ids)
                queue.append(child)
            edges.append((symbol, ids[key]))
        if edges:
            transitions[src] = tuple(edges)

    return MinimalDFA(
        final_states=frozenset(finals),
        transitions=transitions,
        state_count=len(ids),
    )
