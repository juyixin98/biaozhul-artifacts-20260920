"""Deterministic bipartite maximum matching (variables -> values).

Kuhn's augmenting-path algorithm with deterministic visitation order
(variables in the order given, values sorted ascending) so that matchings —
and therefore pruning certificates — are reproducible across runs.
"""
from __future__ import annotations


def maximum_matching(
    variables: list[str], adjacency: dict[str, list[int]]
) -> dict[str, int]:
    """Return a maximum matching as {variable: value}.

    `adjacency[x]` must list the values adjacent to x (any order; sorted here).
    """
    match_value_to_var: dict[int, str] = {}
    match_var_to_value: dict[str, int] = {}

    def augment(var: str, seen: set[int]) -> bool:
        for value in sorted(adjacency[var]):
            if value in seen:
                continue
            seen.add(value)
            other = match_value_to_var.get(value)
            if other is None or augment(other, seen):
                match_value_to_var[value] = var
                match_var_to_value[var] = value
                return True
        return False

    for var in variables:
        augment(var, set())
    return match_var_to_value
