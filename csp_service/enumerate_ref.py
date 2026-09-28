"""Independent brute-force reference enumerator.

This module deliberately shares NO code with the reasoning kernel: it checks
constraints directly (table membership, pairwise inequality for
all-different) over the Cartesian product of the domains. Verification
compares the kernel's output against this enumerator, so a bug in the
kernel's propagation cannot manufacture its own reference answers.
"""
from __future__ import annotations

import itertools

from .model import AllDifferentConstraint, Problem, TableConstraint


def enumerate_solutions(
    problem: Problem, max_assignments: int = 2_000_000
) -> tuple[list[dict[str, int]], bool]:
    """Return (solutions, complete). complete=False means the assignment
    budget was hit and the list is a prefix in lexicographic order."""
    names = [v.name for v in problem.variables]
    domains = [sorted(v.domain) for v in problem.variables]
    tables = [
        (c.vars[0], c.vars[1], frozenset((p[0], p[1]) for p in c.allowed))
        for c in problem.constraints
        if isinstance(c, TableConstraint)
    ]
    alldiffs = [
        c.vars for c in problem.constraints if isinstance(c, AllDifferentConstraint)
    ]
    solutions: list[dict[str, int]] = []
    checked = 0
    for combo in itertools.product(*domains):
        checked += 1
        if checked > max_assignments:
            return solutions, False
        assignment = dict(zip(names, combo))
        if any(
            (assignment[a], assignment[b]) not in allowed for a, b, allowed in tables
        ):
            continue
        if any(
            len({assignment[v] for v in vars_}) != len(vars_) for vars_ in alldiffs
        ):
            continue
        solutions.append(assignment)
    return solutions, True
