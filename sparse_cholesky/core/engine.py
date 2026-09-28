"""High-level factorization engine.

Orchestrates the pipeline mandated by the backend:

    ordering  ->  symmetric permutation (matrix *and* RHS together)
             ->  symbolic factorization (etree + fill), cached by exact pattern
             ->  numeric factorization with pivot localization
             ->  sparse triangular solve
             ->  inverse permutation of the solution

The symbolic and numeric stages are deliberately separate objects so a
symbolic structure can be reused across matrices with identical support.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
from scipy import sparse

from config.settings import FactorizationConfig
from sparse_cholesky.input.errors import FactorizationError
from sparse_cholesky.input.matrix import SparseMatrix

from .cache import CachedSymbolic, SymbolicCache
from .numeric import NumericFactor, numeric_factorization
from .ordering import (
    ORDERING_NATURAL,
    Permutation,
    bandwidth,
    compute_ordering,
    permute_matrix,
    permute_rhs,
    unpermute_solution,
)
from .solve import solve_ldlt
from .symbolic import symbolic_factorization

ProgressHook = Callable[[str, dict], None]


@dataclass
class FactorizationResult:
    numeric: NumericFactor
    permutation: Permutation
    ordering_name: str
    nnz_a_lower: int
    nnz_l: int
    fill_in: int
    bandwidth_before: int
    bandwidth_after: int
    etree_height: int
    cache_hit: bool
    pivots: np.ndarray = field(repr=False)

    def solve(self, b: np.ndarray) -> np.ndarray:
        """Solve ``A x = b`` for one RHS, permuting both sides correctly."""
        b_perm = permute_rhs(b, self.permutation)
        y = solve_ldlt(self.numeric, b_perm)
        return unpermute_solution(y, self.permutation)

    def solve_permuted(self, b_perm: np.ndarray) -> np.ndarray:
        """Solve when the caller already supplied ``P b``."""
        return solve_ldlt(self.numeric, b_perm)


class FactorizationEngine:
    """Stateful engine owning the symbolic-structure cache."""

    def __init__(self, config: FactorizationConfig | None = None,
                 cache: SymbolicCache | None = None) -> None:
        self.config = config if config is not None else FactorizationConfig()
        self.cache = cache if cache is not None else SymbolicCache()

    # ------------------------------------------------------------------
    def factor(
        self,
        matrix: SparseMatrix,
        *,
        ordering: str = ORDERING_NATURAL,
        b: np.ndarray | None = None,
        symbolic_handle: CachedSymbolic | None = None,
        progress: ProgressHook | None = None,
    ) -> tuple[FactorizationResult, np.ndarray | None]:
        """Run the full pipeline; returns ``(result, x_or_None)``.

        ``symbolic_handle`` forces reuse of a previously computed symbolic
        structure; the pattern is verified to match exactly.
        """
        n = matrix.n
        if n > self.config.max_order:
            raise ValueError(
                f"matrix order {n} exceeds configured max_order "
                f"{self.config.max_order}"
            )
        if matrix.nnz_input > self.config.max_nnz:
            raise ValueError(
                f"nnz {matrix.nnz_input} exceeds configured max_nnz "
                f"{self.config.max_nnz}"
            )

        def emit(step: str, **payload) -> None:
            if progress is not None:
                progress(step, payload)

        emit("ordering:start", ordering=ordering, n=n)
        bandwidth_before = bandwidth(matrix.csc)
        permutation = compute_ordering(ordering, matrix.csc)
        permuted = permute_matrix(matrix.csc, permutation)
        bandwidth_after = bandwidth(permuted)
        emit("ordering:done", bandwidth_before=bandwidth_before,
             bandwidth_after=bandwidth_after)

        cache_hit = False
        if symbolic_handle is not None:
            self.cache.require_match(symbolic_handle, permuted)
            cached = symbolic_handle
            cache_hit = True
            emit("symbolic:reuse", nnz_l=cached.symbolic.nnz_lower)
        elif self.config.symbolic_cache_enabled:
            cached = self.cache.get(permuted, ordering)
            if cached is not None:
                cache_hit = True
                emit("symbolic:cache_hit", nnz_l=cached.symbolic.nnz_lower)

        if not cache_hit:
            emit("symbolic:start")
            sym = symbolic_factorization(permuted)
            emit("symbolic:done", nnz_a_lower=sym.nnz_a_lower,
                 nnz_l=sym.nnz_lower, fill_in=sym.fill_in,
                 etree_height=sym.etree_height)
            cached = self.cache.put(permuted, ordering, permutation, sym)

        sym = cached.symbolic

        emit("numeric:start", columns=n)
        try:
            numeric = numeric_factorization(
                permuted,
                sym,
                self.config,
                progress=(lambda step, k, total, piv: emit(
                    "numeric:pivot", column=k, total=total, pivot=piv))
                if progress is not None else None,
            )
        except FactorizationError as exc:
            # Localize the offending pivot in the user's original numbering.
            if exc.pivot_index is not None:
                exc.ordering_index = int(permutation.perm[exc.pivot_index])
            emit("numeric:failed", error_type=exc.error_type,
                 pivot_index=exc.pivot_index,
                 original_pivot_index=exc.ordering_index,
                 pivot_value=exc.pivot_value)
            raise
        emit("numeric:done", min_pivot=float(np.min(numeric.diag)),
             max_pivot=float(np.max(numeric.diag)))

        result = FactorizationResult(
            numeric=numeric,
            permutation=permutation,
            ordering_name=ordering,
            nnz_a_lower=sym.nnz_a_lower,
            nnz_l=sym.nnz_lower,
            fill_in=sym.fill_in,
            bandwidth_before=bandwidth_before,
            bandwidth_after=bandwidth_after,
            etree_height=sym.etree_height,
            cache_hit=cache_hit,
            pivots=numeric.diag,
        )

        x = None
        if b is not None:
            emit("solve:start")
            x = result.solve(np.asarray(b, dtype=np.float64))
            emit("solve:done")
        return result, x

    def symbolic_for(self, matrix: SparseMatrix,
                     ordering: str = ORDERING_NATURAL) -> CachedSymbolic:
        """Expose/prime a reusable symbolic structure for exact-pattern reuse."""
        permutation = compute_ordering(ordering, matrix.csc)
        permuted = permute_matrix(matrix.csc, permutation)
        cached = self.cache.get(permuted, ordering)
        if cached is None:
            sym = symbolic_factorization(permuted)
            cached = self.cache.put(permuted, ordering, permutation, sym)
        return cached
