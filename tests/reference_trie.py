"""独立参考实现：朴素 Trie。

只依赖 Python 标准数据结构，与被测的 minidfa 核心无任何共享代码，
用于交叉验证接受语言与前缀计数。参考答案由此独立实现产生。
"""

from __future__ import annotations

from typing import Iterator


class _Node:
    __slots__ = ("children", "terminal", "subtree_words")

    def __init__(self) -> None:
        self.children: dict[str, _Node] = {}
        self.terminal = False
        self.subtree_words = 0  # 经过该节点的词数（含在该节点终结的词）


class ReferenceTrie:
    def __init__(self, words: list[str] | tuple[str, ...] | set[str]) -> None:
        self._root = _Node()
        for word in set(words):
            self._insert(word)

    def _insert(self, word: str) -> None:
        node = self._root
        node.subtree_words += 1
        for ch in word:
            node = node.children.setdefault(ch, _Node())
            node.subtree_words += 1
        node.terminal = True

    def contains(self, word: str) -> bool:
        node = self._walk(word)
        return node is not None and node.terminal

    def prefix_count(self, prefix: str) -> int:
        node = self._walk(prefix)
        return 0 if node is None else node.subtree_words

    def _walk(self, text: str) -> _Node | None:
        node = self._root
        for ch in text:
            node = node.children.get(ch)
            if node is None:
                return None
        return node

    def words(self) -> list[str]:
        return sorted(self._iter(self._root, ""))

    def _iter(self, node: _Node, prefix: str) -> Iterator[str]:
        if node.terminal:
            yield prefix
        for ch in sorted(node.children):
            yield from self._iter(node.children[ch], prefix + ch)
