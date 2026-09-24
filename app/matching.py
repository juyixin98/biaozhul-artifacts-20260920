"""Minimum-cost bipartite matching (Hungarian algorithm, Kuhn-Munkres).

Generic O(n^3) implementation supporting rectangular matrices and explicit
"infinite" (infeasible) entries. Each row is assigned to a distinct column;
unmatched rows/columns are reported as -1.  Used by the dispatcher for the
explainable global minimum-cost robot->task match.
"""
from __future__ import annotations

import math
from typing import Sequence

INF = math.inf


def hungarian(cost: Sequence[Sequence[float]]) -> tuple[float, list[int]]:
    """Solve a minimum-weight assignment.

    ``cost[r][c]`` may be ``+inf`` for infeasible pairs.  Returns
    ``(total_cost, assignment)`` with ``assignment[r]`` = column assigned to
    row ``r`` or ``-1`` when no feasible column exists for that row.
    The algorithm prefers leaving rows idle over assigning an infinite pair.
    """
    n = len(cost)
    m = max((len(row) for row in cost), default=0)
    if n == 0 or m == 0:
        return 0.0, [-1] * n

    # The augmenting-path core requires rows <= columns; transpose otherwise
    # and map the result back (a column matched to row i means row i takes
    # that column in the original problem).
    if n > m:
        transposed = [
            [cost[r][c] if c < len(cost[r]) else INF for r in range(n)]
            for c in range(m)
        ]
        total, t_assignment = hungarian(transposed)
        assignment = [-1] * n
        for c, r in enumerate(t_assignment):
            if r != -1:
                assignment[r] = c
        return total, assignment

    finite = [v for row in cost for v in row if v != INF and v != math.inf]
    if not finite:
        return 0.0, [-1] * n
    # Replace infinities with a value larger than every finite total, so that
    # a feasible finite assignment always beats a forced infeasible edge.
    big = sum(abs(v) for v in finite) + sum(finite) + 1.0
    a = [[v if v != INF and v != math.inf else big for v in row]
         for row in cost]
    for row in a:
        row += [big] * (m - len(row))

    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)   # p[j] = row assigned to column j (0 = none)
    way = [0] * (m + 1)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [math.inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = math.inf
            j1 = 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = a[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break

    assignment = [-1] * n
    total = 0.0
    for j in range(1, m + 1):
        i = p[j]
        if i:
            raw = cost[i - 1][j - 1] if j - 1 < len(cost[i - 1]) else INF
            if raw == INF or raw == math.inf:
                # Algorithm fell back to an infeasible edge: leave row idle.
                continue
            assignment[i - 1] = j - 1
            total += raw
    return total, assignment


def brute_force_best(cost: Sequence[Sequence[float]]) -> float:
    """Exhaustive optimum for tests/small matrices."""
    import itertools

    n = len(cost)
    m = max((len(row) for row in cost), default=0)
    best = math.inf
    cols = range(m)
    for chosen in itertools.permutations(cols, min(n, m)):
        rows = range(n) if n <= m else range(n)
        # when n > m, some rows unmatched; enumerate which rows are unmatched
        if n > m:
            for active in itertools.combinations(rows, m):
                total = 0.0
                ok = True
                for r, c in zip(active, chosen):
                    val = cost[r][c]
                    if val == math.inf:
                        ok = False
                        break
                    total += val
                if ok:
                    best = min(best, total)
        else:
            total = 0.0
            ok = True
            for r, c in enumerate(chosen):
                val = cost[r][c]
                if val == math.inf:
                    ok = False
                    break
                total += val
            if ok:
                best = min(best, total)
    return best
