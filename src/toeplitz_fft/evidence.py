"""Independent numerical evidence.

The reference answers are **not** produced by the FFT core under test:

* ``mpmath_oracle`` evaluates every output element at high decimal precision
  directly from the definition ``y_i = sum_j T[i, j] x_j`` (pure mpmath,
  independent of NumPy/SciPy arithmetic);
* ``scipy_dense_oracle`` builds the matrix with ``scipy.linalg.toeplitz``
  and does a dense BLAS multiply - a different code path from the embedding
  kernel.

The two oracles are cross-checked against each other; the FFT result is then
compared to both.  A report records concrete error numbers, the tolerance,
the decision basis and, on failure, the explicit category
``numeric_accuracy``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import mpmath  # type: ignore[import-untyped]
import numpy as np
from scipy import linalg as sp_linalg

from .config import SETTINGS
from .errors import ErrorCode
from .inputs import Problem
from .logging_ctx import get_logger

# Relative tolerances per output precision.  FFT double precision is ~1e-14
# relative; these are deliberately looser engineering gates, not tightness
# claims.
REL_TOLERANCE: dict[str, float] = {"double": 1e-9, "single": 1e-5}
ABS_TOLERANCE: dict[str, float] = {"double": 1e-10, "single": 1e-6}


@dataclass(frozen=True)
class ErrorMetrics:
    max_abs_error: float
    max_rel_error: float
    rms_abs_error: float
    scale: float
    tolerance_rel: float
    tolerance_abs: float
    passed: bool
    basis: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_abs_error": self.max_abs_error,
            "max_rel_error": self.max_rel_error,
            "rms_abs_error": self.rms_abs_error,
            "scale": self.scale,
            "tolerance_rel": self.tolerance_rel,
            "tolerance_abs": self.tolerance_abs,
            "passed": self.passed,
            "basis": self.basis,
        }


# --------------------------------------------------------------------------- #
# Independent oracles
# --------------------------------------------------------------------------- #

def _to_mp(value: Any) -> mpmath.mpc | mpmath.mpf:
    """Convert a numpy scalar to high-precision mpmath (exact decimal of repr)."""
    if np.iscomplexobj(value):
        return mpmath.mpc(mpmath.mpf(repr(float(value.real))),
                          mpmath.mpf(repr(float(value.imag))))
    return mpmath.mpf(repr(float(value)))


def mpmath_oracle_element(c_mp: list, r_mp: list, x_mp: list, i: int,
                          n: int, complex_mode: bool):
    """One output element ``y_i`` straight from the definition (mpmath)."""
    total = mpmath.mpc(0) if complex_mode else mpmath.mpf(0)
    for j in range(n):
        coeff = c_mp[i - j] if i >= j else r_mp[j - i]
        total += coeff * x_mp[j]
    return total


def mpmath_sampled_oracle(c: np.ndarray, r: np.ndarray, x: np.ndarray,
                          indices: list[tuple[int, int]]) -> np.ma.MaskedArray:
    """High-precision reference at selected ``(batch, row)`` indices only.

    Returns a masked array the shape of ``x``; unselected positions are
    masked.  This is the large-n evidence path: exact per-element checks
    without an O(n^2) dense reference.
    """
    n = c.shape[0]
    complex_mode = bool(np.iscomplexobj(c))
    out = np.ma.masked_all(x.shape, dtype=np.complex128 if complex_mode
                           else np.float64)
    with mpmath.workdps(SETTINGS.oracle_mpmath_prec):
        c_mp = [_to_mp(v) for v in c]
        r_mp = [_to_mp(v) for v in r]
        for b, i in indices:
            x_mp = [_to_mp(v) for v in x[b]]
            value = mpmath_oracle_element(c_mp, r_mp, x_mp, i, n, complex_mode)
            out[b, i] = complex(value) if complex_mode else float(value)
    return out


def deterministic_sample_indices(n: int, batch: int, count: int,
                                 salt: int = 0) -> list[tuple[int, int]]:
    """Reproducible (b, i) probe set covering edges, middle and random spots."""
    rng = np.random.default_rng(20260927 + n + batch * 1009 + salt)
    wanted = min(count, n * batch)
    picks: set[tuple[int, int]] = set()
    # Always include boundary rows/columns.
    for b in range(min(batch, 2)):
        picks.add((b, 0))
        picks.add((b, n - 1))
    while len(picks) < wanted:
        picks.add((int(rng.integers(0, batch)), int(rng.integers(0, n))))
    return sorted(picks)


def mpmath_oracle(c: np.ndarray, r: np.ndarray, x: np.ndarray,
                  dps: int | None = None) -> np.ndarray:
    """High precision definition-level reference (batch, n)."""
    dps = dps or SETTINGS.oracle_mpmath_prec
    n = c.shape[0]
    complex_mode = bool(np.iscomplexobj(c))
    with mpmath.workdps(dps):
        cmp = [_to_mp(v) for v in c]
        rmp = [_to_mp(v) for v in r]
        out = np.empty(x.shape, dtype=np.complex128 if complex_mode
                       else np.float64)
        for b in range(x.shape[0]):
            xmp = [_to_mp(v) for v in x[b]]
            for i in range(n):
                total = mpmath_oracle_element(cmp, rmp, xmp, i, n,
                                              complex_mode)
                out[b, i] = complex(total) if complex_mode else float(total)
    return np.ascontiguousarray(out)


def scipy_dense_oracle(c: np.ndarray, r: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Dense SciPy reference (independent linalg construction + BLAS gemm)."""
    t = sp_linalg.toeplitz(c, r)
    return np.ascontiguousarray(x @ t.T)


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

def error_metrics(actual: np.ndarray, reference: np.ndarray,
                  precision: str) -> ErrorMetrics:
    work_dtype = np.complex128 if (np.iscomplexobj(actual)
                                   or np.iscomplexobj(reference)) else np.float64
    diff = np.ma.abs(np.ma.asarray(actual).astype(work_dtype)
                     - np.ma.asarray(reference).astype(work_dtype))
    ref_mag = np.ma.abs(np.ma.asarray(reference).astype(work_dtype))
    if diff.count() == 0:
        raise ValueError("cannot compute error metrics: no unmasked samples")
    scale = max(float(ref_mag.max()), 1.0)
    denom = np.ma.maximum(ref_mag, 1e-300)
    rel = diff / denom
    max_abs = float(diff.max())
    max_rel = float(rel.max())
    rms = float(np.ma.sqrt((diff ** 2).mean()))
    tol_rel = REL_TOLERANCE[precision]
    tol_abs = ABS_TOLERANCE[precision]
    # Decision basis: every observed element clears a combined absolute and
    # scale-aware relative gate.
    passed = max_abs <= tol_abs + tol_rel * scale
    return ErrorMetrics(
        max_abs_error=max_abs,
        max_rel_error=max_rel,
        rms_abs_error=rms,
        scale=scale,
        tolerance_rel=tol_rel,
        tolerance_abs=tol_abs,
        passed=passed,
        basis=(f"max_abs={max_abs:.3e} <= tol_abs + tol_rel*scale="
               f"{tol_abs + tol_rel * scale:.3e}"),
    )


# --------------------------------------------------------------------------- #
# Verification report
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class VerificationReport:
    case_id: str
    passed: bool
    failure_category: str | None
    n: int
    batch: int
    mode: str
    precision: str
    kernel_path: str
    cache_hit: bool
    against_mpmath: dict[str, Any]
    against_scipy_dense: dict[str, Any]
    oracle_agreement: dict[str, Any]
    memory: dict[str, Any]
    steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "failure_category": self.failure_category,
            "n": self.n,
            "batch": self.batch,
            "mode": self.mode,
            "precision": self.precision,
            "kernel_path": self.kernel_path,
            "cache_hit": self.cache_hit,
            "against_mpmath": self.against_mpmath,
            "against_scipy_dense": self.against_scipy_dense,
            "oracle_agreement": self.oracle_agreement,
            "memory": self.memory,
            "steps": list(self.steps),
        }


def verify_result(problem: Problem, output: np.ndarray, cache_hit: bool,
                  kernel_path: str, memory: dict[str, Any], *,
                  case_id: str, use_dense_oracle: bool = True,
                  sample_indices: list[tuple[int, int]] | None = None,
                  ) -> VerificationReport:
    """Compare an engine output against the independent oracles.

    When ``sample_indices`` is provided (large-n cases) the mpmath oracle
    evaluates only those ``(batch, row)`` elements at high precision and the
    dense oracle is skipped; the report records the probes explicitly.
    """
    log = get_logger()
    steps: list[str] = []
    n = problem.n

    if sample_indices is not None:
        log.info("verify case=%s step=mpmath_sampled_start n=%d probes=%d",
                 case_id, n, len(sample_indices))
        ref_mp = mpmath_sampled_oracle(
            problem.first_column, problem.first_row, problem.vectors,
            sample_indices)
        steps.append(
            f"mpmath high-precision oracle at {len(sample_indices)} sampled "
            "elements (large-n mode)")
        m_mp = error_metrics(output, ref_mp, problem.precision)
        log.info("verify case=%s step=mpmath_sampled_compare passed=%s %s",
                 case_id, m_mp.passed, m_mp.basis)
        dense = {"used": False, "reason": "large-n sampled mode"}
        oracle_agreement = {"used": False, "reason": "large-n sampled mode"}
        return VerificationReport(
            case_id=case_id,
            passed=m_mp.passed,
            failure_category=None if m_mp.passed
            else ErrorCode.NUMERIC_ACCURACY.value,
            n=n, batch=problem.batch, mode=problem.mode,
            precision=problem.precision, kernel_path=kernel_path,
            cache_hit=cache_hit,
            against_mpmath={**m_mp.to_dict(), "sampled": True,
                            "sample_indices": [list(p) for p in sample_indices],
                            "probe_count": len(sample_indices)},
            against_scipy_dense=dense,
            oracle_agreement=oracle_agreement,
            memory=memory, steps=steps,
        )

    log.info("verify case=%s step=mpmath_oracle_start n=%d batch=%d dps=%d",
             case_id, n, problem.batch, SETTINGS.oracle_mpmath_prec)
    ref_mp = mpmath_oracle(problem.first_column, problem.first_row,
                           problem.vectors)
    steps.append("mpmath high-precision oracle evaluated from definition")
    m_mp = error_metrics(output, ref_mp, problem.precision)
    log.info("verify case=%s step=mpmath_compare passed=%s %s",
             case_id, m_mp.passed, m_mp.basis)

    dense: dict[str, Any] = {"used": False, "reason": None}
    oracle_agreement = {"used": False, "reason": None}
    if use_dense_oracle and n <= SETTINGS.dense_oracle_max_n:
        ref_dense = scipy_dense_oracle(
            problem.first_column.astype(
                np.complex128 if problem.is_complex else np.float64),
            problem.first_row.astype(
                np.complex128 if problem.is_complex else np.float64),
            problem.vectors.astype(
                np.complex128 if problem.is_complex else np.float64),
        )
        steps.append("scipy.linalg.toeplitz dense oracle evaluated")
        m_dense = error_metrics(output, ref_dense, problem.precision)
        dense = {"used": True, **m_dense.to_dict()}
        agree = error_metrics(ref_mp, ref_dense, "double")
        oracle_agreement = {"used": True, **agree.to_dict()}
        log.info("verify case=%s step=scipy_dense_compare passed=%s",
                 case_id, m_dense.passed)
        passed = m_mp.passed and m_dense.passed and agree.passed
    else:
        reason = (f"n={n} > dense_oracle_max_n={SETTINGS.dense_oracle_max_n}"
                  if use_dense_oracle else "dense oracle disabled")
        dense["reason"] = reason
        oracle_agreement["reason"] = reason
        steps.append(f"dense oracle skipped ({reason})")
        passed = m_mp.passed

    return VerificationReport(
        case_id=case_id,
        passed=passed,
        failure_category=None if passed else ErrorCode.NUMERIC_ACCURACY.value,
        n=n,
        batch=problem.batch,
        mode=problem.mode,
        precision=problem.precision,
        kernel_path=kernel_path,
        cache_hit=cache_hit,
        against_mpmath=m_mp.to_dict(),
        against_scipy_dense=dense,
        oracle_agreement=oracle_agreement,
        memory=memory,
        steps=steps,
    )
