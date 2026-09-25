"""Weighted ridge least squares solved with a stacked SVD.

Given design matrix ``A`` (n x p), response ``y`` (n,), non-negative weights
``w`` (n,) and per-parameter penalty ``pen`` (p,) >= 0, solve

    minimize_b  sum_i w_i (y_i - A_i b)^2  +  sum_j pen_j b_j^2

without forming the (possibly rank deficient) normal equations.  Stacking the
weighted rows and the diagonal penalty gives an equivalent ordinary least
squares problem::

    [ diag(sqrt(w)) A ] b ~ [ diag(sqrt(w)) y ]
    [ diag(sqrt(pen))  ]   [ 0                ]

whose coefficient matrix is SVD-decomposed. Rank is read off the singular
values and rank deficiency is therefore handled explicitly instead of
producing unstable large coefficients.
"""

from __future__ import annotations

import numpy as np


class RankDeficientError(np.linalg.LinAlgError):
    """Raised when the (penalized) design matrix does not have full rank.

    Attributes
    ----------
    rank, p:
        Numerical rank and number of columns.
    smallest_singular_value:
        Smallest (rank-th) singular value of the stacked matrix.
    threshold:
        Singular-value cutoff used for the rank decision.
    nullspace_dim:
        Number of near-zero singular directions.
    """

    def __init__(self, rank, p, smallest_sv, threshold, nullspace_dim):
        self.rank = rank
        self.p = p
        self.smallest_singular_value = smallest_sv
        self.threshold = threshold
        self.nullspace_dim = nullspace_dim
        super().__init__(
            f"matrix is rank deficient: numerical rank {rank} / {p} "
            f"(threshold={threshold:.3g}, smallest singular value="
            f"{smallest_sv:.3g}, nullspace dim={nullspace_dim})"
        )


def _default_rcond(shape):
    # Same rule np.linalg.lstsq uses: rcond = max(n, p) * machine epsilon.
    return max(shape) * np.finfo(np.float64).eps


def weighted_ridge_solve(A, y, w=None, pen=None, rcond=None,
                         allow_rank_deficient=False):
    """Minimum-residual (optionally minimum-norm) weighted ridge solution.

    Parameters
    ----------
    A:
        Design matrix, shape (n, p).
    y:
        Response, shape (n,).
    w:
        Non-negative observation weights, shape (n,). ``None`` means all ones.
        Zero weights are allowed (rows are effectively removed); extremely
        small positive weights are floored to zero below
        ``eps * max(weight)`` so a zero-weight row cannot leak in through
        roundoff.
    pen:
        Per-coefficient squared penalties, shape (p,). Zero means the
        corresponding coefficient is *not* penalized — use this for the
        intercept. ``None`` means no penalty anywhere.
    rcond:
        Relative singular-value cutoff. Singular values
        ``<= rcond * s_max`` are treated as zero. Defaults to
        ``max(n+p, p) * eps``.
    allow_rank_deficient:
        If False (default), raise :class:`RankDeficientError` when the rank is
        below p. If True, return the minimum-norm (SVD pseudo-inverse)
        solution of the stacked system and mark ``rank_deficient=True``.

    Returns
    -------
    dict
        ``beta`` (p,), ``rank``, ``singular_values`` (p,), ``threshold``,
        ``rank_deficient`` (bool).
    """
    A = np.asarray(A, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    if A.ndim != 2:
        raise ValueError("A must be 2-D")
    n, p = A.shape
    if y.shape != (n,):
        raise ValueError("y must have shape (n,)")

    if w is None:
        w = np.ones(n, dtype=np.float64)
    else:
        w = np.asarray(w, dtype=np.float64)
        if w.shape != (n,):
            raise ValueError("w must have shape (n,)")
        if np.any(w < 0.0):
            raise ValueError("weights must be non-negative")
    wmax = float(np.max(w)) if n else 0.0
    if wmax > 0.0:
        # Positive-but-roundoff weights behave numerically like zeros.
        active = w > (np.finfo(np.float64).eps * wmax)
    else:
        active = np.zeros(n, dtype=bool)

    if pen is None:
        pen = np.zeros(p, dtype=np.float64)
    else:
        pen = np.asarray(pen, dtype=np.float64)
        if pen.shape != (p,):
            raise ValueError("pen must have shape (p,)")
        if np.any(pen < 0.0):
            raise ValueError("penalties must be non-negative")

    # Build the stacked system [W^{1/2} A; D^{1/2}] b ~ [W^{1/2} y; 0].
    # Rows for zero penalties are all-zero rows: they contribute nothing to
    # the objective nor to the singular values, only document which
    # coefficients are unpenalized (e.g. the intercept).
    if not active.any() and not np.any(pen > 0.0):
        raise ValueError(
            "no active rows and no positive penalty: the system is "
            "unconstrained (all observation weights are zero)"
        )
    sqrtw = np.sqrt(w[active]) if active.any() else np.ones(0)
    M_top = A[active] * sqrtw[:, None] if active.any() else np.zeros((0, p))
    rhs_top = y[active] * sqrtw if active.any() else np.zeros(0)
    M_bot = np.sqrt(pen)
    M = np.vstack([M_top, np.diag(M_bot)])
    rhs = np.concatenate([rhs_top, np.zeros(p)])

    u, s, vh = np.linalg.svd(M, full_matrices=False)
    if rcond is None:
        rcond = _default_rcond(M.shape)
    threshold = rcond * (s[0] if s.size and s[0] > 0.0 else 0.0)
    rank = int(np.count_nonzero(s > threshold))

    rank_deficient = rank < p
    if rank == 0:
        # Everything is in the null space: pseudo-solution is zero, but the
        # problem is structurally degenerate; surface it explicitly.
        if not allow_rank_deficient:
            raise RankDeficientError(0, p, 0.0 if s.size else 0.0,
                                     threshold, p)
        return {
            "beta": np.zeros(p, dtype=np.float64),
            "rank": 0,
            "singular_values": s,
            "threshold": threshold,
            "rank_deficient": True,
        }

    s_inv = np.zeros_like(s)
    s_inv[:rank] = 1.0 / s[:rank]
    beta = (vh.T * s_inv) @ (u.T @ rhs)

    if rank_deficient and not allow_rank_deficient:
        raise RankDeficientError(
            rank=rank,
            p=p,
            smallest_sv=float(s[rank - 1]),
            threshold=float(threshold),
            nullspace_dim=p - rank,
        )

    return {
        "beta": beta,
        "rank": rank,
        "singular_values": s,
        "threshold": float(threshold),
        "rank_deficient": rank_deficient,
    }
