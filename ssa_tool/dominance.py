"""控制流与支配关系分析（在可达子图上计算）。

- 可达性：从入口块出发的前向遍历；
- 逆后序 RPO：后序 DFS 的逆序，保证 idom 迭代算法收敛顺序；
- 直接支配者 idom：Cooper/Harvey/Waterman 的迭代数据流算法；
- 支配边界 DF：Cytron 等经典算法。

不可达块不参与任何计算（它们的前驱里没有入口可达块，对正确性无影响）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .ir import FunctionIR


@dataclass
class DomInfo:
    order: list[str]                 # RPO（可达块）
    reachable: set[str]
    rpo_index: dict[str, int]
    idom: dict[str, str | None]      # 入口为 None
    succ: dict[str, list[str]]
    preds: dict[str, list[str]]

    def dominates(self, a: str, b: str) -> bool:
        """块 a 是否支配块 b（含自身）。"""
        if a == b:
            return True
        cur: str | None = b
        while cur is not None:
            d = self.idom.get(cur)
            if d == a:
                return True
            cur = d
        return False

    def strictly_dominates(self, a: str, b: str) -> bool:
        return a != b and self.dominates(a, b)


def _successors(fn: FunctionIR) -> dict[str, list[str]]:
    labels = {b.name for b in fn.blocks}
    succ: dict[str, list[str]] = {}
    for b in fn.blocks:
        succ[b.name] = [s for s in b.successors() if s in labels]
    return succ


def _reverse_postorder(succ: dict[str, list[str]], entry: str) -> list[str]:
    visited: set[str] = set()
    order: list[str] = []

    def dfs(u: str) -> None:
        visited.add(u)
        for v in succ.get(u, []):
            if v not in visited:
                dfs(v)
        order.append(u)

    dfs(entry)
    order.reverse()
    return order


def _idom_iter(order: list[str], preds: dict[str, list[str]]
               ) -> dict[str, str | None]:
    """Cooper-Harvey-Waterman 迭代求直接支配者。"""
    rpo = {n: i for i, n in enumerate(order)}
    entry = order[0]
    idom: dict[str, str | None] = {n: None for n in order}
    idom[entry] = entry

    def intersect(a: str, b: str) -> str:
        x, y = a, b
        while x != y:
            while rpo[x] > rpo[y]:
                d = idom[x]
                assert d is not None
                x = d
            while rpo[y] > rpo[x]:
                d = idom[y]
                assert d is not None
                y = d
        return x

    changed = True
    while changed:
        changed = False
        for n in order[1:]:
            processed = [p for p in preds.get(n, []) if idom[p] is not None]
            if not processed:
                continue
            new_idom = processed[0]
            for p in processed[1:]:
                new_idom = intersect(p, new_idom)
            if idom[n] != new_idom:
                idom[n] = new_idom
                changed = True

    return {n: (None if n == entry else d) for n, d in idom.items()}


def dominator_tree(idom: dict[str, str | None]) -> dict[str, list[str]]:
    tree: dict[str, list[str]] = {n: [] for n in idom}
    for n, d in idom.items():
        if d is not None:
            tree[d].append(n)
    return tree


def dominance_frontiers(order: list[str], idom: dict[str, str | None],
                        preds: dict[str, list[str]]
                        ) -> dict[str, set[str]]:
    """Cytron 支配边界：DF[n] = {j : n 支配 j 的某个前驱且不严格支配 j}。"""
    df: dict[str, set[str]] = {n: set() for n in order}
    for n in order:
        for p in preds.get(n, []):
            runner: str | None = p
            while runner is not None and runner != idom.get(n):
                df[runner].add(n)
                runner = idom.get(runner)
    return df


def analyze(fn: FunctionIR) -> DomInfo:
    succ_all = _successors(fn)
    order = _reverse_postorder(succ_all, fn.entry.name)
    reachable = set(order)
    preds: dict[str, list[str]] = {n: [] for n in order}
    for u in order:
        for v in succ_all.get(u, []):
            if v in reachable:
                preds[v].append(u)
    idom = _idom_iter(order, preds)
    return DomInfo(order=order, reachable=reachable,
                   rpo_index={n: i for i, n in enumerate(order)},
                   idom=idom, succ=succ_all, preds=preds)
