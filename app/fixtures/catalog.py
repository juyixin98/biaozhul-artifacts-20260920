"""Reusable synthetic CSP fixtures.

Every fixture is a plain JSON-compatible dictionary (the same shape accepted
by the HTTP API) so tests and the independent oracle can consume it without
instantiating solver classes. Known reference facts (enumerated solution
count, expected root-propagation verdicts) are attached separately in
:mod:`app.fixtures.catalog`; they were produced by brute-force enumeration
(see ``tests/oracle.py``), never by the solver kernel.
"""

from __future__ import annotations

import itertools
import random
from typing import Any

# ---------------------------------------------------------------------------
# Relation construction helpers (pure data, no solver imports)
# ---------------------------------------------------------------------------


def comparison(op: str) -> dict[str, Any]:
    return {"kind": "comparison", "pairs": [], "op": op}


def allowed(*pairs: tuple[int, int]) -> dict[str, Any]:
    return {"kind": "allowed", "pairs": [list(pair) for pair in pairs]}


def forbidden(*pairs: tuple[int, int]) -> dict[str, Any]:
    return {"kind": "forbidden", "pairs": [list(pair) for pair in pairs]}


def binary(left: str, right: str, relation: dict[str, Any]) -> dict[str, Any]:
    return {"left": left, "right": right, "relation": relation}


def neq_pairs(domain: list[int]) -> dict[str, Any]:
    """All-equal pairs forbidden: the ``x != y`` relation over one domain."""
    return forbidden(*[(value, value) for value in domain])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def hall_conflict() -> dict[str, Any]:
    """Three variables constrained pairwise-different inside two values.

    This is a pure Hall violation with no binary constraints: pairwise value
    deletion does nothing on the initial domains, only matching/Hall
    reasoning proves infeasibility.
    """
    return {
        "name": "hall_conflict",
        "domains": {"a": [1, 2], "b": [1, 2], "c": [1, 2]},
        "binary_constraints": [],
        "all_different": [["a", "b", "c"]],
    }


def induced_hall_conflict() -> dict[str, Any]:
    """A Hall set that only appears after binary propagation.

    Binary allowed-pair tables force a, b and c each into {1,2}; only then
    does all-different over {a,b,c} hit a Hall violation (3 variables, 2
    values). Pure matching on the initial domains would find a perfect
    matching, so the conflict depends on propagation order.
    """
    one_two = allowed((1, 0), (2, 0))
    return {
        "name": "induced_hall_conflict",
        "domains": {
            "a": [1, 2, 3],
            "b": [1, 2, 3],
            "c": [1, 2, 3],
            "x": [0],
        },
        "binary_constraints": [
            binary("a", "x", one_two),
            binary("b", "x", one_two),
            binary("c", "x", one_two),
        ],
        "all_different": [["a", "b", "c"]],
    }


def isolated_variable() -> dict[str, Any]:
    """An unconstrained variable must keep every domain value.

    ``a`` and ``b`` are all-different over {1,2}; ``lonely`` appears in no
    constraint and must not be pruned at all.
    """
    return {
        "name": "isolated_variable",
        "domains": {"a": [1, 2], "b": [1, 2], "lonely": [7, 8, 9]},
        "binary_constraints": [],
        "all_different": [["a", "b"]],
    }


def multiple_solutions() -> dict[str, Any]:
    """Three all-different variables on {1,2,3}: exactly 6 solutions."""
    return {
        "name": "multiple_solutions",
        "domains": {"x": [1, 2, 3], "y": [1, 2, 3], "z": [1, 2, 3]},
        "binary_constraints": [],
        "all_different": [["x", "y", "z"]],
    }


def coloring_three_nodes() -> dict[str, Any]:
    """Path a-b-c with 3 colors: 12 of 27 colorings satisfy both edges."""
    colors = [1, 2, 3]
    return {
        "name": "coloring_three_nodes",
        "domains": {"a": colors, "b": colors, "c": colors},
        "binary_constraints": [
            binary("a", "b", neq_pairs(colors)),
            binary("b", "c", neq_pairs(colors)),
        ],
        "all_different": [],
    }


def unsat_mixed() -> dict[str, Any]:
    """x != y from all-different but x == y from a binary equality."""
    return {
        "name": "unsat_mixed",
        "domains": {"x": [1, 2], "y": [1, 2]},
        "binary_constraints": [binary("x", "y", comparison("eq"))],
        "all_different": [["x", "y"]],
    }


def unsat_arc_only() -> dict[str, Any]:
    """x<y and y<x: infeasibility proven by binary arc consistency alone."""
    return {
        "name": "unsat_arc_only",
        "domains": {"x": [1, 2, 3], "y": [1, 2, 3]},
        "binary_constraints": [
            binary("x", "y", comparison("lt")),
            binary("y", "x", comparison("lt")),
        ],
        "all_different": [],
    }


def forced_chain() -> dict[str, Any]:
    """Inequality chain d1<d2<d3<d4 on {1..4}: unique solution 1,2,3,4.

    Root propagation alone assigns everything; exercises reason output.
    """
    return {
        "name": "forced_chain",
        "domains": {f"d{i}": [1, 2, 3, 4] for i in range(1, 5)},
        "binary_constraints": [
            binary(f"d{i}", f"d{i + 1}", comparison("lt")) for i in range(1, 4)
        ],
        "all_different": [["d1", "d2", "d3", "d4"]],
    }


def queens(n: int) -> dict[str, Any]:
    """N-queens on columns 1..n.

    all_different enforces distinct rows; forbidden pairs on each column
    pair exclude equal rows and both diagonals. n=4 needs deep
    backtracking; n=8 much more.
    """
    columns = [f"q{i}" for i in range(n)]
    domains = {column: list(range(1, n + 1)) for column in columns}
    constraints: list[dict[str, Any]] = []
    for i, left in enumerate(columns):
        for j in range(i + 1, n):
            right = columns[j]
            gap = j - i
            clashes: set[tuple[int, int]] = set()
            for row in range(1, n + 1):
                clashes.add((row, row))
                if 1 <= row + gap <= n:
                    clashes.add((row, row + gap))
                if 1 <= row - gap <= n:
                    clashes.add((row, row - gap))
            constraints.append(
                binary(left, right, forbidden(*sorted(clashes)))
            )
    return {
        "name": f"queens_{n}",
        "domains": domains,
        "binary_constraints": constraints,
        "all_different": [columns],
    }


def overlapping_groups() -> dict[str, Any]:
    """Two overlapping all-different groups sharing variable b."""
    return {
        "name": "overlapping_groups",
        "domains": {
            "a": [1, 2],
            "b": [1, 2, 3],
            "c": [1, 2],
            "d": [2, 3],
        },
        "binary_constraints": [],
        "all_different": [["a", "b"], ["b", "c", "d"]],
    }


def explicit_relation_table() -> dict[str, Any]:
    """Allowed-pair table: s chooses a pair (1,1)/(2,3)/(3,2) between u,v."""
    return {
        "name": "explicit_relation_table",
        "domains": {"u": [1, 2, 3], "v": [1, 2, 3], "w": [0, 1]},
        "binary_constraints": [
            binary(
                "u",
                "v",
                allowed((1, 1), (2, 3), (3, 2)),
            ),
            binary("w", "u", comparison("le")),
        ],
        "all_different": [],
    }


def deep_backtrack_sat() -> dict[str, Any]:
    """Unique-solution permutation instance that needs 20 backtracks.

    Five all-different variables on {1..5}; sparse forbidden tables leave a
    unique solution (verified by full enumeration). ``v0`` participates in
    no binary constraint (isolated except for all-different). Search space
    is only 5**5 = 3125 assignments, so the oracle can enumerate it, while
    the solver must still perform deep backtracking.
    """
    return {
        "name": "deep_backtrack_sat",
        "domains": {f"v{i}": [1, 2, 3, 4, 5] for i in range(5)},
        "binary_constraints": [
            binary("v2", "v4", forbidden(
                (1, 2), (2, 1), (2, 2), (3, 4), (3, 5),
                (4, 2), (4, 3), (5, 1), (5, 3), (5, 4), (5, 5))),
            binary("v2", "v3", forbidden(
                (1, 2), (1, 3), (2, 2), (3, 2), (3, 5),
                (4, 1), (4, 3), (5, 5))),
            binary("v3", "v4", forbidden(
                (1, 2), (2, 1), (2, 3), (2, 4), (2, 5), (3, 1),
                (4, 1), (4, 4), (4, 5), (5, 2), (5, 3), (5, 4), (5, 5))),
            binary("v1", "v4", forbidden(
                (1, 2), (2, 3), (2, 5), (3, 1), (3, 5),
                (4, 2), (4, 5), (5, 2), (5, 3), (5, 4))),
            binary("v1", "v3", forbidden(
                (2, 1), (2, 2), (3, 3), (3, 4),
                (4, 3), (4, 5), (5, 2), (5, 5))),
            binary("v1", "v2", forbidden(
                (1, 1), (1, 2), (1, 5), (2, 2), (2, 3), (2, 4), (2, 5),
                (4, 1), (4, 2), (5, 1), (5, 4))),
        ],
        "all_different": [["v0", "v1", "v2", "v3", "v4"]],
    }


def deep_backtrack_unsat() -> dict[str, Any]:
    """Small (288 assignments) infeasible instance needing ~80 backtracks.

    Equality v0 == v2 and all-different over {v0,v2,v4} force v4 to differ
    from two identical variables; the unrestricted v1, v3 domains make the
    contradiction reachable only after exploring many branches, so this
    exercises full domain/queue restoration across deep backtracking.
    """
    return {
        "name": "deep_backtrack_unsat",
        "domains": {
            "v0": [1, 2, 3, 4],
            "v1": [1, 2],
            "v2": [1, 2, 3, 4],
            "v3": [2, 3, 4],
            "v4": [1, 2, 4],
        },
        "binary_constraints": [
            binary("v0", "v2", comparison("eq")),
            binary("v0", "v4", comparison("ne")),
        ],
        "all_different": [["v0", "v2", "v4"]],
    }


def seeded_random_instance(seed: int = 20260928) -> dict[str, Any]:
    """Deterministic mixed random instance for repeatable verification."""
    rng = random.Random(seed)
    n = 5
    names = [f"v{i}" for i in range(n)]
    domains = {
        name: sorted(rng.sample(range(0, 6), rng.randint(2, 4))) for name in names
    }
    ops = ["eq", "ne", "lt", "le", "gt", "ge"]
    constraints: list[dict[str, Any]] = []
    pairs = list(itertools.combinations(names, 2))
    rng.shuffle(pairs)
    for left, right in pairs[:3]:
        if rng.random() < 0.6:
            relation = comparison(rng.choice(ops))
        else:
            table = [
                (a, b)
                for a in domains[left]
                for b in domains[right]
                if rng.random() < 0.6
            ]
            relation = allowed(*table) if table else forbidden()
        constraints.append(binary(left, right, relation))
    return {
        "name": f"seeded_random_{seed}",
        "domains": domains,
        "binary_constraints": constraints,
        "all_different": [rng.sample(names, 3)],
    }


CATALOG: dict[str, callable] = {
    "hall_conflict": hall_conflict,
    "induced_hall_conflict": induced_hall_conflict,
    "isolated_variable": isolated_variable,
    "multiple_solutions": multiple_solutions,
    "coloring_three_nodes": coloring_three_nodes,
    "unsat_mixed": unsat_mixed,
    "unsat_arc_only": unsat_arc_only,
    "forced_chain": forced_chain,
    "queens_4": lambda: queens(4),
    "queens_8": lambda: queens(8),
    "deep_backtrack_sat": deep_backtrack_sat,
    "deep_backtrack_unsat": deep_backtrack_unsat,
    "overlapping_groups": overlapping_groups,
    "explicit_relation_table": explicit_relation_table,
    "seeded_random": seeded_random_instance,
}

# Fixtures whose full assignment space is too large for brute-force
# enumeration. They are checked for status/solution validity, not against the
# complete enumerated solution set.
NOT_ENUMERABLE = {"queens_8"}


def get_fixture(name: str) -> dict[str, Any]:
    if name not in CATALOG:
        raise KeyError(name)
    return CATALOG[name]()


def all_fixtures() -> dict[str, dict[str, Any]]:
    return {name: builder() for name, builder in CATALOG.items()}
