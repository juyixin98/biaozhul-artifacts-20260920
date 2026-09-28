"""Union-find for equivalent classes.

Unlike a plain union-find, every successful merge records *why* it happened
(the equivalence axiom source and the two operands it connected). Merging
never discards that provenance: the full merge chain can be replayed.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MergeRecord:
    a: str
    b: str
    source: str  # originating equivalence axiom's declared source


class UnionFind:
    def __init__(self, elements: set[str]):
        self._parent = {x: x for x in elements}
        self.merge_log: list[MergeRecord] = []

    def add(self, x: str) -> None:
        self._parent.setdefault(x, x)

    def find(self, x: str) -> str:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # path compression (does not affect merge_log provenance)
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str, source: str) -> bool:
        """Merge ``a`` and ``b``; record provenance on a real merge."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        # Attach rb under ra; record at class-name level so the chain shows
        # the exact operands the axiom named.
        self._parent[rb] = ra
        self.merge_log.append(MergeRecord(a=a, b=b, source=source))
        return True

    def components(self) -> dict[str, frozenset[str]]:
        groups: dict[str, set[str]] = {}
        for x in self._parent:
            groups.setdefault(self.find(x), set()).add(x)
        return {r: frozenset(m) for r, m in groups.items()}
