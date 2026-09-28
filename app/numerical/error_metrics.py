"""Backward-error evidence, computed per right-hand-side column.

Two standard quantities (see Higham, *Accuracy and Stability of Numerical
Algorithms*):

* normwise relative backward error

      eta_j = ||r_j||_inf / (||A||_inf ||x_j||_inf + ||b_j||_inf)

  the smallest relative perturbation of A and b (measured in the infinity
  norm) for which x_j is the exact solution.

* componentwise backward error (Oettli-Prager)

      omega_j = max_i |r_{ij}| / ( (|A| |x_j|)_i + |b_{ij}| ).

All evaluation is in mpmath at the caller's precision; the residual passed in
must have been formed from the *original* matrix.
"""

from __future__ import annotations

from dataclasses import dataclass

from mpmath import mpf

from . import mp
from . import arithmetic as arith


@dataclass(frozen=True)
class ColumnError:
    column: int
    residual_inf: mpf
    b_inf: mpf
    x_inf: mpf
    eta_normwise: mpf
    omega_componentwise: mpf

    def as_dict(self) -> dict[str, str]:
        return {
            "column": self.column,
            "residual_inf": mp.nstr(self.residual_inf, 12),
            "b_inf": mp.nstr(self.b_inf, 12),
            "x_inf": mp.nstr(self.x_inf, 12),
            "eta_normwise": mp.nstr(self.eta_normwise, 6),
            "omega_componentwise": mp.nstr(self.omega_componentwise, 6),
        }


def column_errors(a, x, b, r) -> list[ColumnError]:
    """Evaluate both backward errors independently for every RHS column."""
    n = a.rows
    nrhs = b.cols
    a_inf = arith.infinity_norm(a)
    reports: list[ColumnError] = []
    for j in range(nrhs):
        r_col = [abs(r[i, j]) for i in range(n)]
        x_col = [x[i, j] for i in range(n)]
        b_col = [b[i, j] for i in range(n)]
        residual_inf = max(r_col, default=mpf(0))
        x_inf = max((abs(v) for v in x_col), default=mpf(0))
        b_inf = max((abs(v) for v in b_col), default=mpf(0))

        denom_norm = a_inf * x_inf + b_inf
        eta = residual_inf / denom_norm if denom_norm != 0 else mpf(0)

        omega = mpf(0)
        for i in range(n):
            ax_abs = mpf(0)
            for k in range(n):
                ax_abs += abs(a[i, k]) * abs(x_col[k])
            denom = ax_abs + abs(b_col[i])
            if denom != 0:
                ratio = r_col[i] / denom
                if ratio > omega:
                    omega = ratio
        reports.append(
            ColumnError(
                column=j,
                residual_inf=residual_inf,
                b_inf=b_inf,
                x_inf=x_inf,
                eta_normwise=eta,
                omega_componentwise=omega,
            )
        )
    return reports
