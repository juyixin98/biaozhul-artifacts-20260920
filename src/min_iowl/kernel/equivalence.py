"""Equivalence-class partitioning with provenance preservation.

Two named classes end up in one equivalence class when each strictly entails
the other through the saturated TBox (``C in supers(D)`` and ``D in supers(C)``),
which covers both direct ``EquivalentClasses`` axioms and equivalences induced
through intersection/subclass chains.

Merging is read-only aggregation: the original axioms (including every
``EquivalentClasses`` source) stay stored individually; a merge only records
*which* declared axioms justify it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..lang.compiler import CompiledOntology
from .engine import SaturationReport

__all__ = ["EquivalenceGroup", "build_equivalence_groups"]


@dataclass(frozen=True, slots=True)
class EquivalenceGroup:
    canonical: str
    members: tuple[str, ...]
    # Axiom ids that justify collapsing these members (direct + indirect).
    supporting_axioms: tuple[str, ...]
    # Only the direct EquivalentClasses declarations contributing members.
    direct_equivalence_axioms: tuple[str, ...]


def _union_find_pairs(nodes: list[str], supers: dict[str, frozenset[str]]) -> list[frozenset[str]]:
    parent = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, a in enumerate(nodes):
        for b in nodes[i + 1 :]:
            if b in supers.get(a, frozenset()) and a in supers.get(b, frozenset()):
                union(a, b)

    groups: dict[str, list[str]] = {}
    for n in nodes:
        groups.setdefault(find(n), []).append(n)
    return [frozenset(v) for v in groups.values()]


def build_equivalence_groups(
    program: CompiledOntology, report: SaturationReport
) -> tuple[EquivalenceGroup, ...]:
    nodes = sorted(c for c in program.declared_classes)
    raw_groups = _union_find_pairs(nodes, report.tbox_supers)

    # Map every EquivalentClasses axiom to the named classes it mentions.
    direct_axioms: dict[str, list[str]] = {}
    axiom_by_id = {a.axiom_id: a for a in program.axioms}
    for name, source_ids in program.class_sources.items():
        for sid in source_ids:
            record = axiom_by_id.get(sid)
            if record is not None and record.kind == "EquivalentClasses":
                direct_axioms.setdefault(sid, []).append(name)

    result: list[EquivalenceGroup] = []
    for group in raw_groups:
        members = tuple(sorted(group))
        canonical = members[0]
        # Provenance: union of all class_sources of all members, then keep only
        # sources that actually connect members (justification), plus all direct
        # EquivalentClasses axioms mentioning any member.
        support: set[str] = set()
        direct: set[str] = set()
        for member in members:
            for sid in program.class_sources.get(member, ()):  # type: ignore[arg-type]
                support.add(sid)
                record = axiom_by_id.get(sid)
                if record is not None and record.kind == "EquivalentClasses":
                    direct.add(sid)
        result.append(
            EquivalenceGroup(
                canonical=canonical,
                members=members if len(members) > 1 else members,
                supporting_axioms=tuple(sorted(support)),
                direct_equivalence_axioms=tuple(sorted(direct)),
            )
        )
    # Groups with >=2 members first, then singletons alphabetically.
    result.sort(key=lambda g: (len(g.members) == 1, g.canonical))
    return tuple(result)
