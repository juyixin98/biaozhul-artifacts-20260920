"""参考 Trie —— 独立的朴素实现，用于交叉验证 DAWG。

本实现刻意不依赖 :mod:`app.core.dawg`：它用嵌套 dict 的朴素前缀树，
成员判定与前缀计数逻辑各自独立编写。测试用它验证 DAWG 的接受语言与
前缀计数，避免"参考答案全部由被测核心自身生成"。
"""

from typing import Iterable


class ReferenceTrie:
    """朴素 dict 前缀树。

    结构约定：每个节点是 ``dict``；终结节点包含哨兵键 ``""``。
    """

    _END = ""

    def __init__(self, words: Iterable[str] = ()) -> None:
        self._root: dict = {}
        self._size = 0
        for word in words:
            self.insert(word)

    def insert(self, word: str) -> bool:
        node = self._root
        for ch in word:
            nxt = node.get(ch)
            if nxt is None:
                nxt = {}
                node[ch] = nxt
            node = nxt
        if self._END in node:
            return False
        node[self._END] = True
        self._size += 1
        return True

    def __len__(self) -> int:
        return self._size

    def contains(self, word: str) -> bool:
        node = self._root
        for ch in word:
            node = node.get(ch)  # type: ignore[assignment]
            if node is None:
                return False
        return self._END in node

    def _node_at(self, text: str) -> dict | None:
        node = self._root
        for ch in text:
            node = node.get(ch)
            if node is None:
                return None
        return node

    def prefix_count(self, prefix: str) -> int:
        node = self._node_at(prefix)
        if node is None:
            return 0
        return self._subtree_size(node)

    def _subtree_size(self, node: dict) -> int:
        total = 1 if self._END in node else 0
        for key, child in node.items():
            if key != self._END:
                total += self._subtree_size(child)
        return total

    def accepted_words(self, domain: Iterable[str]) -> list[str]:
        """在给定候选域上返回被接受的词，用于语言等价比对。"""
        return [w for w in domain if self.contains(w)]

    def node_count(self) -> int:
        """统计节点数（含根），仅用于诊断输出。"""
        return self._count_nodes(self._root)

    def _count_nodes(self, node: dict) -> int:
        total = 1
        for key, child in node.items():
            if key != self._END:
                total += self._count_nodes(child)
        return total
