"""Factorization service engine.

Orchestrates the pipeline:

    COO input -> validation -> ordering -> symbolic factor
             -> numeric LDL^T -> solve -> independent evidence

Symbolic factors are cached and reused **only** when the sparsity
pattern is byte-for-byte identical (dimension + pattern fingerprint),
per the reuse rule. Different numerical values with the same pattern
reuse the symbolic structure; any pattern change forces a new one.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp

from ..config import settings
from ..core import ordering as ordering_mod
from ..core.numerical import ldlt_factor, solve_original
from ..core.symbolic import SymbolicFactor, pattern_hash, symbolic_factor
from ..errors import ErrorCode, PatternMismatchError, SparseSpdError
from ..evidence import metrics
from ..numerical_input.sparse_matrix import SparseInput, from_coo
from ..runlog import RunLogger, fingerprint


@dataclass(frozen=True)
class FactorReport:
    run_id: str
    n: int
    ordering: str
    nnz_input: int
    nnz_orig_lower: int
    nnz_l: int
    fill_entries: int
    fill_ratio: float
    elapsed_symbolic: float
    elapsed_numeric: float
    elapsed_total: float
    pivot_count: int
    pattern_reused: bool
    pattern_fingerprint: str
    ordering_fill_est: int


@dataclass(frozen=True)
class SolveResult:
    x: np.ndarray
    report: FactorReport
    evidence: metrics.Evidence
    factor: object


class _SymbolicCache:
    """Small LRU keyed by (n, pattern hash, ordering method)."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._data: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def _key(self, n: int, fp: str, method: str) -> tuple:
        return (n, fp, method)

    def get(self, n: int, upper: sp.csr_matrix, method: str):
        fp = pattern_hash(upper)
        key = self._key(n, fp, method)
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None, fp
            self._data.move_to_end(key)
            return item, fp

    def put(self, n: int, fp: str, method: str, sym: SymbolicFactor) -> None:
        key = self._key(n, fp, method)
        with self._lock:
            self._data[key] = sym
            self._data.move_to_end(key)
            while len(self._data) > self._capacity:
                self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)


class FactorizationEngine:
    def __init__(self, *, enable_cache: bool = True) -> None:
        self.cache = _SymbolicCache(settings.cache_max_entries) \
            if enable_cache else None

    # ------------------------------------------------------------------
    def factorize(self, n: int, rows, cols, vals, *,
                  ordering: str = "minimum_degree",
                  run_id: str | None = None,
                  logger: RunLogger | None = None) -> tuple:
        """Return (sparse_input, symbolic, numeric, report, logger)."""
        fp = fingerprint(n, rows, cols, vals)
        log = logger or RunLogger(fp=fp)
        if run_id is not None:
            log.run_id = run_id
        t0 = time.perf_counter()

        sin: SparseInput = from_coo(n, rows, cols, vals)
        log.banner(sin.n, sin.nnz_input)

        order = ordering_mod.compute_ordering(
            ordering, *self._upper_edges(sin.upper), sin.n)
        log.info("ordering_done", method=order.method,
                 simulated_fill=order.fill_edges_added)

        reused = False
        sym = None
        if self.cache is not None:
            sym, _fp = self.cache.get(sin.n, sin.upper, order.method)
            if sym is not None:
                # Defensive strict equality check before reuse.
                if not (sym.n == sin.n
                        and sym.pattern_fingerprint == _fp):
                    raise PatternMismatchError(
                        "cached symbolic pattern does not match",
                        details={"cached_fp": sym.pattern_fingerprint,
                                 "input_fp": _fp})
                reused = True
                log.info("symbolic_reused", pattern_fingerprint=_fp,
                         nnz_l=sym.nnz_lower)

        if sym is None:
            t_sym0 = time.perf_counter()
            sym = symbolic_factor(sin.upper, order)
            t_sym = time.perf_counter() - t_sym0
            log.info("symbolic_done", nnz_l=sym.nnz_lower,
                     fill=sym.fill_entries,
                     elapsed_seconds=round(t_sym, 6),
                     etree_roots=int(np.sum(sym.etree_parent == -1)))
            if self.cache is not None:
                self.cache.put(sin.n, sym.pattern_fingerprint,
                               order.method, sym)
        else:
            t_sym = 0.0

        b = metrics.permute(metrics.full_symmetric(sin.upper),
                            order.perm)
        try:
            fac = ldlt_factor(b, sym, logger=log)
        except SparseSpdError:
            raise

        total = time.perf_counter() - t0
        report = FactorReport(
            run_id=log.run_id,
            n=sin.n,
            ordering=order.method,
            nnz_input=sin.nnz_input,
            nnz_orig_lower=sym.nnz_orig_lower + sin.n,
            nnz_l=sym.nnz_lower,
            fill_entries=sym.fill_entries,
            fill_ratio=sym.nnz_lower
            / max(1, sym.nnz_orig_lower + sin.n),
            elapsed_symbolic=t_sym,
            elapsed_numeric=fac.elapsed_seconds,
            elapsed_total=total,
            pivot_count=sin.n,
            pattern_reused=reused,
            pattern_fingerprint=sym.pattern_fingerprint,
            ordering_fill_est=order.fill_edges_added,
        )
        log.info("numeric_done", nnz_l=sym.nnz_lower,
                 elapsed_seconds=round(fac.elapsed_seconds, 6))
        return sin, sym, fac, report, log

    def solve(self, n: int, rows, cols, vals, rhs, *,
              ordering: str = "minimum_degree",
              run_id: str | None = None,
              with_mpmath: bool = True) -> SolveResult:
        sin, sym, fac, report, log = self.factorize(
            n, rows, cols, vals, ordering=ordering, run_id=run_id)
        rhs_arr = np.asarray(rhs, dtype=np.float64)
        if rhs_arr.shape != (sin.n,):
            from ..errors import InputValidationError
            raise InputValidationError(
                ErrorCode.SIZE_MISMATCH,
                f"rhs length {rhs_arr.shape} != n {sin.n}")
        x = solve_original(fac, rhs_arr)
        ev = metrics.evaluate(fac, sin.upper, rhs_arr, x,
                              with_mpmath=with_mpmath)
        log.info("evidence", residual_rel=ev.residual_rel,
                 reconstruction=ev.reconstruction_abs,
                 dense_err=ev.dense_solution_error,
                 mpmath_err=ev.mpmath_solution_error,
                 passed=ev.passed, reasons=list(ev.reasons))
        return SolveResult(x=x, report=report, evidence=ev, factor=fac)

    @staticmethod
    def _upper_edges(upper: sp.csr_matrix):
        c = upper.tocoo()
        return c.row.astype(np.int64), c.col.astype(np.int64)

    # ------------------------------------------------------------------
    def compare_orderings(self, n: int, rows, cols, vals,
                          methods=("natural", "minimum_degree",
                                   "nested_dissection"),
                          run_id: str | None = None) -> list[dict]:
        """Report fill produced by each ordering (numerics use natural)."""
        sin = from_coo(n, rows, cols, vals)
        out: list[dict] = []
        for m in methods:
            order = ordering_mod.compute_ordering(
                m, *self._upper_edges(sin.upper), sin.n)
            sym = symbolic_factor(sin.upper, order)
            out.append({
                "method": m,
                "nnz_l": sym.nnz_lower,
                "fill_entries": sym.fill_entries,
                "fill_ratio": sym.nnz_lower
                / max(1, sym.nnz_orig_lower + sin.n),
            })
        return out


# A process-wide default engine (symbolic cache shared across requests).
default_engine = FactorizationEngine()
