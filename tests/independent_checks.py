"""Independent numerical checks used by tests.

These deliberately do NOT import the engine or its error module: the backward
error and forward error are recomputed from first principles here, so the
tests are not asserting the implementation against its own arithmetic.
"""

from __future__ import annotations

import mpmath

from app.numerical import mpf, workprec

INDEPENDENT_DPS = 140


def independent_eta(a, x_column, b_column, dps: int = INDEPENDENT_DPS):
    """Normwise relative backward error, independently recomputed."""
    n = a.rows
    with workprec(dps):
        r_inf = mpf(0)
        a_inf = mpf(0)
        x_inf = max((abs(x_column[i]) for i in range(n)), default=mpf(0))
        b_inf = max((abs(b_column[i]) for i in range(n)), default=mpf(0))
        for i in range(n):
            row_sum = mpf(0)
            ri = b_column[i] - sum(
                (a[i, k] * x_column[k] for k in range(n)), mpf(0)
            )
            if abs(ri) > r_inf:
                r_inf = abs(ri)
            for k in range(n):
                row_sum += abs(a[i, k])
            if row_sum > a_inf:
                a_inf = row_sum
        denom = a_inf * x_inf + b_inf
        return r_inf / denom if denom != 0 else mpf(0)


def independent_forward_error(x_computed, x_reference):
    n = x_reference.rows
    with workprec(INDEPENDENT_DPS):
        col = [mpf(str(x_computed[i, 0])) for i in range(n)]
        ref_col = [x_reference[i, 0] for i in range(n)]
        diff = max(abs(col[i] - ref_col[i]) for i in range(n))
        norm = max(abs(v) for v in ref_col)
        return diff / norm if norm != 0 else diff


def parse_solution_column(solution_rows, j=0):
    return [mpf(row[j]) for row in solution_rows]


def matrix_from_column(column):
    return mpmath.matrix([[v] for v in column])
