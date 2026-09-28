"""Independent brute-force oracle for ATMS ground truth.

This module deliberately shares **no reasoning code** with the production
kernel (``atms_backend.core``).  Given a theory expressed as plain Python
tuples it enumerates every subset of assumptions, computes the Horn
closure of each subset from scratch, and derives:

* minimal *consistent* supporting environments per node;
* minimal nogood environments;
* whether a node holds under a fixed assumption context;
* labels after withdrawing assumptions.

Because it is exponential and recomputes closures naively it is useless
in production -- which is exactly why it makes a trustworthy test
oracle: a bug shared by two independently written algorithms is far less
likely than a bug in one.
"""

from __future__ import annotations

from itertools import combinations
from typing import Dict, FrozenSet, Iterable, List, Mapping, Sequence, Set, Tuple

# A theory is plain data, parsed independently of the production DSL:
#   assumptions: frozenset of node ids
#   facts:       frozenset of node ids
#   rules:       tuple of (antecedent_tuple, consequent)
Theory = Tuple[FrozenSet[str], FrozenSet[str], Tuple[Tuple[Tuple[str, ...], str], ...]]


def closure(
    held: FrozenSet[str], rules: Sequence[Tuple[Tuple[str, ...], str]]
) -> FrozenSet[str]:
    """Naive forward-chaining fixpoint for one assumed set."""
    facts: Set[str] = set(held)
    changed = True
    while changed:
        changed = False
        for ants, cons in rules:
            if cons not in facts and all(a in facts for a in ants):
                facts.add(cons)
                changed = True
    return frozenset(facts)


def all_subsets(items: Iterable[str]) -> List[FrozenSet[str]]:
    items = tuple(sorted(items))
    subs: List[FrozenSet[str]] = []
    for size in range(len(items) + 1):
        for combo in combinations(items, size):
            subs.append(frozenset(combo))
    return subs


def _minimal(sets: Iterable[FrozenSet[str]]) -> List[FrozenSet[str]]:
    sets = sorted(sets, key=lambda s: (len(s), sorted(s)))
    out: List[FrozenSet[str]] = []
    for s in sets:
        if not any(o <= s for o in out):
            out.append(s)
    return out


class Oracle:
    """Ground-truth tables for a theory (and its retraction variants)."""

    def __init__(
        self,
        assumptions: Sequence[str],
        facts: Sequence[str],
        rules: Sequence[Tuple[Sequence[str], str]],
    ) -> None:
        self.assumptions = frozenset(assumptions)
        self.facts = frozenset(facts)
        self.rules = tuple((tuple(a), c) for a, c in rules)
        self._tables: Dict[FrozenSet[str], "_Table"] = {}

    def _table(self, assumptions: FrozenSet[str]) -> "_Table":
        if assumptions in self._tables:
            return self._tables[assumptions]
        supports: Dict[str, Set[FrozenSet[str]]] = {}
        nogood_raw: List[FrozenSet[str]] = []
        for subset in all_subsets(assumptions):
            held = self.facts | subset
            reached = closure(held, self.rules)
            if "FALSE" in reached:
                nogood_raw.append(subset)
                continue  # inconsistent subsets support nothing
            for node in reached:
                supports.setdefault(node, set()).add(subset)
        # Facts are supported by the empty environment explicitly.
        for f in self.facts:
            supports.setdefault(f, set()).add(frozenset())
        # Nodes only reachable through inconsistent subsets have no label.
        minimal_nogoods = _minimal(nogood_raw)
        table = _Table(supports, set(minimal_nogoods))
        self._tables[assumptions] = table
        return table

    @property
    def full(self) -> "_Table":
        return self._table(self.assumptions)

    def label(self, node: str) -> List[FrozenSet[str]]:
        return self.full.label(node)

    def nogoods(self) -> List[FrozenSet[str]]:
        return self.full.nogoods

    def holds(self, node: str, context: FrozenSet[str]) -> bool:
        """Ground truth for "node under the given assumption context"."""
        # A context containing a nogood is itself inconsistent: nothing
        # is accepted within it.
        if any(ng <= context for ng in self.nogoods()):
            return False
        return any(env <= context for env in self.label(node))

    def withdrawn(self, removed: Sequence[str]) -> "_Table":
        remaining = self.assumptions - frozenset(removed)
        return self._table(remaining)


class _Table:
    def __init__(
        self,
        raw_supports: Mapping[str, Set[FrozenSet[str]]],
        nogoods: Set[FrozenSet[str]],
    ) -> None:
        self._labels: Dict[str, List[FrozenSet[str]]] = {}
        for node, envs in raw_supports.items():
            # Drop environments blocked by a nogood before minimization.
            clean = [
                e for e in envs if not any(ng <= e for ng in nogoods)
            ]
            self._labels[node] = _minimal(clean)
        self._nogoods = sorted(nogoods, key=lambda e: (len(e), sorted(e)))

    def label(self, node: str) -> List[FrozenSet[str]]:
        return list(self._labels.get(node, []))

    @property
    def nogoods(self) -> List[FrozenSet[str]]:
        return list(self._nogoods)

    def nodes(self) -> List[str]:
        return sorted(n for n, envs in self._labels.items() if envs)
