"""独立的最小 DFA 预言机（测试辅助，不导入被测内核）。

用 Myhill–Nerode 等价类的**划分细化**（partition refinement）计算一个
词集对应完整 DFA 的最小状态数。它直接从词集构造朴素前缀树、补一个死状态，
再迭代按 (终结性, 各符号目标等价类) 签名细分，直到稳定。

该实现与 :mod:`app.core.dawg` 没有任何代码共享，用于独立验证：

1. DAWG 输出的状态数等于理论最小状态数（最小性）；
2. 等价判据必须同时含终结标记与转移——这正是划分细化签名的两个组成部分。
"""

from __future__ import annotations


class _Node:
    __slots__ = ("edges", "final")

    def __init__(self) -> None:
        self.edges: dict[str, "_Node"] = {}
        self.final: bool = False


def _build_trie(words: list[str]) -> _Node:
    root = _Node()
    for word in words:
        node = root
        for ch in word:
            nxt = node.edges.get(ch)
            if nxt is None:
                nxt = _Node()
                node.edges[ch] = nxt
            node = nxt
        node.final = True
    return root


def minimal_state_count(words: list[str]) -> int:
    """返回词集的最小完整 DFA 的可达状态数（含死状态；空词集为 1）。

    注意：这是**完整**自动机（缺边指向死状态）。被测 DAWG 只保存存活状态，
    当语言对某符号存在"前缀可继续"与"前缀必断"两种残余时会省去死状态；
    因此比较时由调用方使用 :func:`minimal_live_state_count`。
    """
    root = _build_trie(words)

    # 枚举 trie 节点（根为 0）。
    nodes: list[_Node] = []
    stack = [root]
    identity: dict[int, int] = {}
    while stack:
        node = stack.pop()
        if id(node) in identity:
            continue
        identity[id(node)] = len(nodes)
        nodes.append(node)
        # 顺序无关，最终签名会重排。
        stack.extend(node.edges.values())
    # 保证根为 0。
    root_index = identity[id(root)]
    nodes[0], nodes[root_index] = nodes[root_index], nodes[0]
    identity = {id(node): i for i, node in enumerate(nodes)}

    alphabet = sorted({ch for node in nodes for ch in node.edges})
    n = len(nodes)
    dead = n
    total = n + 1

    # 转移表（完整 DFA）：缺边 -> dead；dead 自环。
    transitions = [[dead] * len(alphabet) for _ in range(total)]
    finals = [False] * total
    for i, node in enumerate(nodes):
        finals[i] = node.final
        for j, symbol in enumerate(alphabet):
            child = node.edges.get(symbol)
            if child is not None:
                transitions[i][j] = identity[id(child)]

    # 划分细化：初始按终结性分两类。
    classes = [1 if finals[i] else 0 for i in range(total)]
    while True:
        signatures = [
            (classes[i], tuple(classes[transitions[i][j]] for j in range(len(alphabet))))
            for i in range(total)
        ]
        signature_to_class: dict[tuple, int] = {}
        next_classes = [0] * total
        count = 0
        for i, signature in enumerate(signatures):
            if signature not in signature_to_class:
                signature_to_class[signature] = count
                count += 1
            next_classes[i] = signature_to_class[signature]
        if next_classes == classes:
            break
        classes = next_classes

    # 只保留从根（完整 DFA 中）可达的等价类。
    reachable_states = {0}
    stack = [0]
    while stack:
        i = stack.pop()
        for j in range(len(alphabet)):
            target = transitions[i][j]
            if target not in reachable_states:
                reachable_states.add(target)
                stack.append(target)
    return len({classes[i] for i in reachable_states})


def minimal_live_state_count(words: list[str]) -> int:
    """返回与被测 DAWG 口径一致的最小"存活"状态数。

    被测 DAWG 不显式保存死状态，而以"断边"表达拒绝。对有限非空语言：
    完整最小 DFA 中死状态必从根可达（最深的终结态没有出边，必走向死状态），
    而 DAWG 省略它后接受同一语言，故存活状态数 = 完整最小状态数 - 1。

    两个退化情形：

    * 空词集：只有 1 个非终结根状态；
    * 语言仅含空词（字母表为空）：完整 DFA 本就不需要死状态，为 1 个
      终结根状态。
    """
    if not words:
        return 1
    if not any(word for word in words):
        # 词表非空但全部是空串，等价于语言 {""}。
        return 1
    return minimal_state_count(words) - 1
