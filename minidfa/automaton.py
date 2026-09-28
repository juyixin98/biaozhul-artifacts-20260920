"""索引与模型层：冻结的最小无环确定自动机及其查询语义。

状态用整数 id 表示，0 固定为初始状态。转移表为不可变映射。
前缀计数语义：prefix_count(p) = 词典中以 p 为前缀的词的个数，
等于从 p 到达的状态出发可接受的词数（含 p 本身，若该状态终结）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Mapping

from .errors import InvalidSymbolError

START_STATE = 0


@dataclass(frozen=True)
class MinimalDFA:
    """不可变最小无环确定自动机。

    transitions[src] 是按符号排序的 ((symbol, dst), ...) 元组。
    构建后不允许修改；查询均为只读。
    """

    final_states: frozenset[int]
    transitions: Mapping[int, tuple[tuple[str, int], ...]]
    state_count: int

    def __post_init__(self) -> None:
        for src, edges in self.transitions.items():
            for symbol, _ in edges:
                if len(symbol) != 1:
                    raise InvalidSymbolError(
                        f"transition symbol must be a single character, got {symbol!r}",
                        detail={"state": src, "symbol": symbol},
                    )

    # ------------------------------------------------------------------
    # 基本查询
    # ------------------------------------------------------------------

    def _walk(self, text: str) -> int | None:
        """沿 text 转移，返回到达的状态；路径不存在返回 None。"""
        state = START_STATE
        for ch in text:
            edges = self.transitions.get(state)
            if not edges:
                return None
            nxt = None
            for symbol, dst in edges:
                if symbol == ch:
                    nxt = dst
                    break
            if nxt is None:
                return None
            state = nxt
        return state

    def contains(self, word: str) -> bool:
        """word 是否属于该自动机接受的语言。空词看初始状态是否终结。"""
        state = self._walk(word)
        return state is not None and state in self.final_states

    def prefix_count(self, prefix: str) -> int:
        """词典中以 prefix 为前缀的词数。路径不存在时返回 0。"""
        state = self._walk(prefix)
        if state is None:
            return 0
        return self._accepted_count(state, {})

    def _accepted_count(self, state: int, memo: dict[int, int]) -> int:
        """从 state 出发可接受的词数。无环图上记忆化递归，每个词恰计一次。"""
        cached = memo.get(state)
        if cached is not None:
            return cached
        total = 1 if state in self.final_states else 0
        for _, dst in self.transitions.get(state, ()):
            total += self._accepted_count(dst, memo)
        memo[state] = total
        return total

    def iter_words(self) -> Iterator[str]:
        """按字典序枚举接受语言中的全部词。"""
        yield from self._iter_from(START_STATE, "")

    def _iter_from(self, state: int, prefix: str) -> Iterator[str]:
        if state in self.final_states:
            yield prefix
        for symbol, dst in self.transitions.get(state, ()):
            yield from self._iter_from(dst, prefix + symbol)

    def __len__(self) -> int:
        """接受语言的词数。"""
        return self._accepted_count(START_STATE, {})

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def edge_rows(self) -> list[tuple[int, str, int]]:
        """展开为 (src, symbol, dst) 行，供持久化层写入。"""
        rows: list[tuple[int, str, int]] = []
        for src in sorted(self.transitions):
            for symbol, dst in self.transitions[src]:
                rows.append((src, symbol, dst))
        return rows
