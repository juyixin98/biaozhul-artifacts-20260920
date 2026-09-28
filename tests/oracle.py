"""Independent oracle: brute-force enumeration of assumption combinations.

This module deliberately shares NO code with app.core.engine. It computes
expected labels by enumerating every subset of the active assumptions,
running naive forward chaining for each subset, and taking subset-minimal
environments -- the reference answers the engine is checked against.
"""

from __future__ import annotations

from itertools import combinations

CONTRADICTION = "⊥"


def _derive(assumption_subset: frozenset, premises: set, rules: list) -> set:
    """Naive forward chaining to fixpoint (independent of the ATMS kernel)."""
    facts = set(premises) | set(assumption_subset)
    changed = True
    while changed:
        changed = False
        for rule in rules:
            if rule["consequent"] in facts:
                continue
            if all(a in facts for a in rule["antecedents"]):
                facts.add(rule["consequent"])
                changed = True
    return facts


def _subsets(names: list) -> list[frozenset]:
    out = []
    for size in range(len(names) + 1):
        out.extend(frozenset(c) for c in combinations(names, size))
    return out


def _minimal(envs: set) -> set:
    ordered = sorted(envs, key=len)
    kept = []
    for env in ordered:
        if not any(prev <= env for prev in kept):
            kept.append(env)
    return set(kept)


def oracle(assumptions: list, premises: set, rules: list) -> dict:
    """Return {"labels": {node: set[frozenset]}, "nogoods": set[frozenset]}.

    Nogoods are the subset-minimal assumption sets that derive ⊥.
    A node's label is the set of subset-minimal *consistent* (non-nogood)
    assumption sets deriving it.
    """
    derived = {s: _derive(s, premises, rules) for s in _subsets(list(assumptions))}
    nogoods = _minimal({s for s, facts in derived.items() if CONTRADICTION in facts})
    consistent = [s for s in derived if not any(ng <= s for ng in nogoods)]

    nodes = set(premises) | set(assumptions)
    for rule in rules:
        nodes.update(rule["antecedents"])
        nodes.add(rule["consequent"])
    nodes.discard(CONTRADICTION)

    labels = {}
    for node in sorted(nodes):
        envs = {s for s in consistent if node in derived[s]}
        minimal = _minimal(envs)
        if minimal:
            labels[node] = minimal
    return {"labels": labels, "nogoods": nogoods}
