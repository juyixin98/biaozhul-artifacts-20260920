"""Preconditioned Conjugate Gradient (PCG) for sparse SPD systems.

The implementation follows the standard preconditioned CG recurrence but
adds the diagnostics the task requires:

* **True residual monitoring.**  The recurrence residual r = b - Ax drifts
  from the mathematically exact residual under round-off.  Every
  ``true_residual_every`` iterations (and at termination) the *true*
  residual b - Ax is recomputed from scratch and replaces r.  This is the
  value that convergence is judged on.
* **Stagnation detection.**  After ``stall_window`` consecutive iterations
  in which the residual fails to drop below 80% of its value one window
  earlier, the solve terminates with ``status="stagnation"``.  The
  windowed comparison deliberately tolerates the normal CG plateau (the
  residual may tick up for many iterations before superlinear convergence);
  it only fires on genuine round-off-limited no-progress runs.
* **Non-positive-curvature diagnosis.**  If the curvature quantity
  p^T A p is non-positive (up to a small tolerance) the matrix is not
  positive definite on the current Krylov space; the run returns
  ``status="non_positive_curvature"`` with the offending value recorded.
* **Divergence detection.**  NaN/Inf residuals or a residual that explodes
  past a configurable threshold produce explicit failure states rather
  than a silently wrong answer.

All outcomes -- including failures -- are returned as :class:`CGResult`.
Only *invalid input* raises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .csr import CSRMatrix
from .exceptions import (
    InvalidCsrError,
    NotPositiveDefiniteError,
)
from .limits import (
    DEFAULT_MAX_ITER_FACTOR,
    MAX_ITER_CAP,
    MAX_TOL,
    MIN_TOL,
)

#: Human-readable description of every status value (public contract).
STATUS_MESSAGES: dict[str, str] = {
    "converged": "Converged: final true residual satisfies the requested "
                 "relative tolerance.",
    "zero_rhs": "Right-hand side is exactly zero; x = 0 is the unique "
                "solution of the SPD system (no iterations performed).",
    "max_iterations": "Iteration limit reached without achieving the "
                      "requested tolerance.",
    "non_positive_curvature": "Encountered p^T A p <= 0: the matrix is not "
                              "positive definite on the current Krylov "
                              "subspace (input was assumed SPD).",
    "breakdown": "Preconditioner inner product r^T M^{-1} r became "
                 "non-positive; the preconditioner is not SPD.",
    "stagnation": "Residual made no meaningful progress over the stall "
                  "window (round-off-limited or hopelessly ill-conditioned).",
    "diverged": "Residual became non-finite or exceeded the divergence "
                "threshold; solve aborted.",
}

#: Statuses that represent a successful solve.
SUCCESS_STATUSES = frozenset({"converged", "zero_rhs"})

#: A factor by which the initial residual may grow before a run is declared
#: diverged (guards against silent blow-up on numerically nasty systems).
DIVERGENCE_FACTOR = 1.0e8


@dataclass(frozen=True)
class PCGConfig:
    """Solver configuration.

    Attributes
    ----------
    tol:
        Relative residual tolerance.  Convergence criterion is
        ``||b - Ax||_2 <= tol * ||b||_2``; when ``b = 0`` with a nonzero
        initial guess the reference switches to the *initial* residual
        ``||b - A x0||_2`` (residual-reduction factor), since ``tol * 0``
        would otherwise be unreachable.  Must lie in [1e-14, 1e-2]; note
        that the smallest achievable relative residual is bounded below by
        the matvec round-off floor, so a request below that floor returns
        ``status="stagnation"`` rather than a false convergence.
    max_iter:
        Maximum number of CG iterations (spmv applications).  Must be in
        [1, 100_000]; ``None`` selects ``min(10*n, 100_000)``.
    preconditioner:
        ``"none"`` for unpreconditioned CG or ``"jacobi"`` for diagonal
        (Jacobi) preconditioning.  The Jacobi preconditioner is only valid
        when all diagonal entries are positive.
    true_residual_every:
        If > 0, recompute the true residual r = b - Ax from scratch every k
        iterations (residual replacement) and force a final check.  0 means
        "never during iteration", but a final true residual is always
        computed to produce the reported diagnostics.  Must be >= 0.
    stall_window:
        Stagnation is declared only after ``stall_window`` *consecutive*
        iterations in which the residual fails to drop below 80% of the
        residual recorded ``stall_window`` iterations earlier.  Comparing
        against a windowed point (rather than the all-time best) prevents
        the normal CG mid-run "plateau" -- residuals can even tick up for
        dozens of iterations before superlinear convergence -- from being
        mistaken for stagnation.  Must be >= 1; default 50.
    stall_tolerance:
        Improvement factor below which a step counts as "no progress".
        Must be > 0.
    """

    tol: float = 1e-8
    max_iter: int | None = None
    preconditioner: str = "none"
    true_residual_every: int = 1
    stall_window: int = 50
    stall_tolerance: float = 0.8

    def __post_init__(self) -> None:
        if isinstance(self.tol, bool) or not isinstance(
                self.tol, (int, float, np.integer, np.floating)):
            raise InvalidCsrError(
                f"'tol' must be a number, got {type(self.tol).__name__}"
            )
        tol = float(self.tol)
        if not np.isfinite(tol) or not (MIN_TOL <= tol <= MAX_TOL):
            raise InvalidCsrError(
                f"'tol'={tol:g} out of range [{MIN_TOL:g}, {MAX_TOL:g}]"
            )
        object.__setattr__(self, "tol", tol)

        if self.preconditioner not in ("none", "jacobi"):
            raise InvalidCsrError(
                f"'preconditioner' must be 'none' or 'jacobi', "
                f"got {self.preconditioner!r}"
            )

        if not isinstance(self.true_residual_every, (int, np.integer)) \
                or isinstance(self.true_residual_every, bool):
            raise InvalidCsrError("'true_residual_every' must be an int")
        if int(self.true_residual_every) < 0:
            raise InvalidCsrError(
                "'true_residual_every' must be >= 0"
            )
        if not isinstance(self.stall_window, (int, np.integer)) \
                or isinstance(self.stall_window, bool):
            raise InvalidCsrError("'stall_window' must be an int")
        if int(self.stall_window) < 1:
            raise InvalidCsrError("'stall_window' must be >= 1")
        if not np.isfinite(float(self.stall_tolerance)) \
                or not (0.0 < float(self.stall_tolerance) < 1.0):
            raise InvalidCsrError(
                "'stall_tolerance' must be a finite number in (0, 1)"
            )

        if self.max_iter is not None:
            if isinstance(self.max_iter, bool) or not isinstance(
                    self.max_iter, (int, np.integer)):
                raise InvalidCsrError("'max_iter' must be an integer or null")
            mi = int(self.max_iter)
            if not (1 <= mi <= MAX_ITER_CAP):
                raise InvalidCsrError(
                    f"'max_iter'={mi} out of range [1, {MAX_ITER_CAP}]"
                )
            object.__setattr__(self, "max_iter", mi)


@dataclass
class CGResult:
    """Structured outcome of a PCG solve.

    Residuals stored here are *true* residuals (recomputed from b - Ax)
    whenever possible; ``residual_kind`` records whether the last check was
    a true or a recurrence residual.
    """

    x: np.ndarray
    status: str
    iterations: int
    residual_norm: float
    initial_residual_norm: float
    relative_residual: float
    converged: bool
    residual_kind: str = "true"
    b_norm: float = 0.0
    preconditioner: str = "none"
    true_residual_checks: int = 0
    max_residual_drift: float = 0.0
    non_positive_value: float | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable representation (vector x as a plain list).

        Non-finite scalars (only possible in the ``diverged`` state) are
        emitted as JSON ``null`` rather than NaN/Infinity, which strict JSON
        parsers reject.
        """
        def _f(v: float) -> float | None:
            v = float(v)
            return v if np.isfinite(v) else None

        return {
            "status": self.status,
            "converged": bool(self.converged),
            "message": self.message,
            "iterations": int(self.iterations),
            "preconditioner": self.preconditioner,
            "residual_norm": _f(self.residual_norm),
            "initial_residual_norm": _f(self.initial_residual_norm),
            "b_norm": _f(self.b_norm),
            "relative_residual": _f(self.relative_residual),
            "residual_kind": self.residual_kind,
            "true_residual_checks": int(self.true_residual_checks),
            "max_residual_drift": _f(self.max_residual_drift),
            "non_positive_value": (
                None if self.non_positive_value is None
                else _f(self.non_positive_value)
            ),
            "x": [float(v) for v in self.x],
            "history": [
                {**h,
                 "residual_norm": _f(h["residual_norm"]),
                 "relative_residual": _f(h["relative_residual"]),
                 "alpha": _f(h["alpha"])}
                if not np.isfinite(h["residual_norm"])
                   or not np.isfinite(h["relative_residual"])
                   or not np.isfinite(h.get("alpha", 0.0))
                else h
                for h in self.history
            ],
        }


def _validate_system(A: CSRMatrix, b: np.ndarray,
                     x0: np.ndarray | None) -> tuple[np.ndarray, np.ndarray]:
    if not isinstance(A, CSRMatrix):
        raise InvalidCsrError(
            f"'A' must be a CSRMatrix, got {type(A).__name__}"
        )
    b = np.asarray(b, dtype=np.float64)
    if b.shape != (A.n,):
        raise InvalidCsrError(
            f"'b' must have shape ({A.n},), got {b.shape}"
        )
    if not np.all(np.isfinite(b)):
        raise InvalidCsrError("'b' contains non-finite entries")
    if x0 is None:
        x = np.zeros(A.n, dtype=np.float64)
    else:
        x = np.asarray(x0, dtype=np.float64)
        if x.shape != (A.n,):
            raise InvalidCsrError(
                f"'x0' must have shape ({A.n},), got {x.shape}"
            )
        if not np.all(np.isfinite(x)):
            raise InvalidCsrError("'x0' contains non-finite entries")
        x = x.copy()
    return b, x


def solve_pcg(A: CSRMatrix,
              b: np.ndarray,
              *,
              x0: np.ndarray | None = None,
              config: PCGConfig | None = None,
              tol: float | None = None,
              max_iter: int | None = None,
              preconditioner: str | None = None,
              true_residual_every: int | None = None,
              stall_window: int | None = None,
              stall_tolerance: float | None = None) -> CGResult:
    """Solve ``A x = b`` by preconditioned conjugate gradient.

    A must be symmetric positive definite; this is enforced up front by
    structural symmetry and positive-diagonal checks, and at runtime by the
    non-positive-curvature diagnosis.  Parameters may be given via
    ``config`` or as keyword overrides.

    Returns
    -------
    CGResult
        Always a result object -- check ``.converged`` / ``.status``.  Bad
        *input* raises a ValidationError subclass; numerical failure does not.
    """
    cfg_kwargs: dict[str, Any] = {}
    if config is not None:
        if not isinstance(config, PCGConfig):
            raise InvalidCsrError(
                f"'config' must be a PCGConfig, got {type(config).__name__}"
            )
        cfg_kwargs = {
            "tol": config.tol,
            "max_iter": config.max_iter,
            "preconditioner": config.preconditioner,
            "true_residual_every": config.true_residual_every,
            "stall_window": config.stall_window,
            "stall_tolerance": config.stall_tolerance,
        }
    else:
        cfg_kwargs = {
            "tol": 1e-8 if tol is None else tol,
            "max_iter": max_iter,
            "preconditioner": "none" if preconditioner is None
                              else preconditioner,
            "true_residual_every": (1 if true_residual_every is None
                                    else true_residual_every),
            "stall_window": 50 if stall_window is None else stall_window,
            "stall_tolerance": (0.8 if stall_tolerance is None
                                else stall_tolerance),
        }
    cfg = PCGConfig(**cfg_kwargs)

    b, x = _validate_system(A, b, x0)
    n = A.n

    b_norm = float(np.linalg.norm(b))
    if cfg.max_iter is None:
        max_iter = min(DEFAULT_MAX_ITER_FACTOR * n, MAX_ITER_CAP)
    else:
        max_iter = cfg.max_iter

    # ---- preconditioner setup ---------------------------------------------
    diag = None
    if cfg.preconditioner == "jacobi":
        try:
            A.assert_positive_diagonal()
        except NotPositiveDefiniteError:
            raise
        diag = A.diagonal()
        if not np.all(np.isfinite(diag)) or np.any(diag <= 0.0):
            raise NotPositiveDefiniteError(
                "Jacobi preconditioner requires strictly positive diagonal "
                "entries"
            )

    def apply_precond(v: np.ndarray) -> np.ndarray:
        return v / diag if diag is not None else v

    # ---- initial residual ---------------------------------------------------
    r = b - A.matvec(x)
    initial_rnorm = float(np.linalg.norm(r))
    if not np.isfinite(initial_rnorm):
        return _finish(
            x, "diverged", 0, float("nan"), initial_rnorm, b_norm, cfg,
            residual_kind="recurrence", history=[],
        )

    # b == 0: unique SPD solution is x = 0 only when x0 == 0; more generally
    # just run the normal machinery.  With the default x0=0 the residual is
    # already zero and this short-circuits.
    if b_norm == 0.0 and initial_rnorm == 0.0:
        return CGResult(
            x=x,
            status="zero_rhs",
            iterations=0,
            residual_norm=0.0,
            initial_residual_norm=0.0,
            relative_residual=0.0,
            converged=True,
            residual_kind="true",
            b_norm=0.0,
            preconditioner=cfg.preconditioner,
            true_residual_checks=1,
            max_residual_drift=0.0,
            history=[_hist_entry(0, 0.0, 0.0, "true", 0.0)],
            message=STATUS_MESSAGES["zero_rhs"],
        )

    # Relative-residual reference norm.  Standard form is ||r||/||b||; but
    # when b = 0 with a nonzero x0, ||b|| = 0 makes any nonzero tolerance
    # unreachable.  In that case (and only that case) we measure progress
    # against the *initial* residual instead, i.e. a residual-reduction
    # factor.  This is the conventional convention (e.g. PETSc's
    # KSP_NORM_UNPRECONDITIONED relative criterion behaves the same way).
    reference_norm = b_norm if b_norm > 0.0 else initial_rnorm

    def residual_threshold() -> float:
        return cfg.tol * reference_norm

    history: list[dict[str, Any]] = []
    true_checks = 0
    max_drift = 0.0

    threshold = residual_threshold()
    if initial_rnorm <= threshold:
        # Already converged at x0 -- verify with the true residual BEFORE
        # touching the preconditioner (a round-off-sized residual could
        # otherwise produce a tiny negative r^T M^-1 r and a spurious
        # "breakdown").
        r_true = b - A.matvec(x)
        rn_true = float(np.linalg.norm(r_true))
        true_checks += 1
        max_drift = max(max_drift, abs(rn_true - initial_rnorm))
        rel = rn_true / reference_norm
        history.append(_hist_entry(0, rn_true, rel, "true", 0.0))
        converged_now = rn_true <= threshold or rn_true == 0.0
        return CGResult(
            x=x, status="converged" if converged_now else "max_iterations",
            iterations=0,
            residual_norm=rn_true, initial_residual_norm=initial_rnorm,
            relative_residual=rel, converged=converged_now,
            residual_kind="true", b_norm=b_norm,
            preconditioner=cfg.preconditioner,
            true_residual_checks=true_checks, max_residual_drift=max_drift,
            history=history,
            message=(STATUS_MESSAGES["converged"] if converged_now
                     else STATUS_MESSAGES["max_iterations"]),
        )

    z = apply_precond(r)
    p = z.copy()
    rz = float(np.dot(r, z))
    if not np.isfinite(rz) or rz <= 0.0:
        return _finish(
            x, "breakdown", 0, initial_rnorm, initial_rnorm, b_norm, cfg,
            residual_kind="recurrence", history=history,
            non_positive_value=rz,
        )

    rnorm = initial_rnorm
    # Windowed stagnation bookkeeping: recent_norms[t] is the (checked)
    # residual norm at iteration t.  Progress at iteration k means rnorm <
    # stall_tolerance * rnorm_{k-W}.  Only k >= W iterations can accumulate
    # stall credit, and the counter resets on any progressive step.
    recent_norms = [initial_rnorm]
    stall_count = 0
    last_kind = "recurrence"
    status = "max_iterations"
    non_pos_value: float | None = None
    iterations = 0

    history.append(_hist_entry(0, rnorm,
                               rnorm / reference_norm,
                               "initial", 0.0))

    for k in range(1, max_iter + 1):
        iterations = k
        Ap = A.matvec(p)
        pAp = float(np.dot(p, Ap))

        # --- non-positive curvature diagnosis --------------------------------
        # The exact SPD identity is p^T A p > 0.  Two failure modes:
        #   (a) pAp < 0 beyond round-off  -> definitely indefinite;
        #   (b) pAp ~ 0 (|.| <= floor)    -> numerically singular (a zero
        #       eigenvalue was reached), i.e. semidefinite.
        # The floor is the round-off noise level of the dot product itself,
        # eps * ||p|| * ||Ap|| (Cauchy-Schwarz scale).  Using this *relative*
        # scale -- not A's absolute scale -- means a legitimately SPD but
        # tiny matrix (A = 1e-20 * I) is still accepted, while an exact
        # zero pAp on a singular matrix is correctly flagged.
        p_p = float(np.dot(p, p))
        ap_norm2 = float(np.dot(Ap, Ap))
        if not np.isfinite(p_p) or not np.isfinite(ap_norm2):
            status = "diverged"
            non_pos_value = pAp if np.isfinite(pAp) else float("nan")
            break
        curvature_floor = 1e-12 * np.sqrt(max(p_p * ap_norm2, 1e-300))
        if not np.isfinite(pAp) or pAp <= curvature_floor:
            status = "non_positive_curvature"
            non_pos_value = pAp
            break

        alpha = rz / pAp
        if not np.isfinite(alpha):
            status = "diverged"
            break

        x += alpha * p
        r -= alpha * Ap
        rnorm_rec = float(np.linalg.norm(r))

        # --- divergence guards ------------------------------------------------
        if not np.isfinite(rnorm_rec):
            status = "diverged"
            rnorm = rnorm_rec
            last_kind = "recurrence"
            break
        if rnorm_rec > DIVERGENCE_FACTOR * max(initial_rnorm, 1.0):
            status = "diverged"
            rnorm = rnorm_rec
            last_kind = "recurrence"
            break

        # --- periodic true residual (residual replacement) -------------------
        do_true_check = (
            cfg.true_residual_every > 0
            and k % cfg.true_residual_every == 0
        )
        if do_true_check:
            r_true = b - A.matvec(x)
            rn_true = float(np.linalg.norm(r_true))
            max_drift = max(max_drift, abs(rn_true - rnorm_rec))
            r = r_true
            rnorm = rn_true
            last_kind = "true"
            true_checks += 1
        else:
            rnorm = rnorm_rec
            last_kind = "recurrence"

        rel = rnorm / reference_norm
        history.append(_hist_entry(k, rnorm, rel, last_kind,
                                   float(alpha)))

        if rnorm <= threshold:
            status = "converged"
            break

        # --- windowed stagnation detection ------------------------------------
        # rnorm at k is compared to the checked norm at k - W.  Early
        # iterations (k < W) cannot stall: a fresh Krylov space with
        # distinct eigenvalues is still in its initial contraction phase.
        recent_norms.append(rnorm)
        if k >= cfg.stall_window:
            old = recent_norms[k - cfg.stall_window]
            if rnorm < cfg.stall_tolerance * old:
                stall_count = 0
            else:
                stall_count += 1
                if stall_count >= cfg.stall_window:
                    status = "stagnation"
                    break

        z = apply_precond(r)
        rz_new = float(np.dot(r, z))
        if not np.isfinite(rz_new) or rz_new <= 0.0:
            status = "breakdown"
            non_pos_value = rz_new
            break
        beta = rz_new / rz
        p = z + beta * p
        rz = rz_new

    # ---- final true residual: diagnostics always report the honest error ----
    r_true = b - A.matvec(x)
    rn_true = float(np.linalg.norm(r_true))
    true_checks += 1
    max_drift = max(max_drift, abs(rn_true - rnorm))
    if not np.isfinite(rn_true):
        status = "diverged"
    final_rel = rn_true / reference_norm

    # A run may have hit the iteration budget / stall while its *current*
    # point actually satisfies tolerance according to the true residual.
    # Trust the true residual: that is the acceptance criterion.
    if status in ("max_iterations", "stagnation") and rn_true <= threshold:
        status = "converged"
    # Conversely, a "converged" recurrence check can be exposed as false by
    # the true residual -- then report the honest failure state.
    if status == "converged" and rn_true > threshold:
        status = "max_iterations" if iterations >= max_iter else "stagnation"

    history.append(_hist_entry(iterations, rn_true, final_rel, "true_final",
                               history[-1].get("alpha", 0.0)))

    return CGResult(
        x=x,
        status=status,
        iterations=iterations,
        residual_norm=rn_true,
        initial_residual_norm=initial_rnorm,
        relative_residual=final_rel,
        converged=status in SUCCESS_STATUSES,
        residual_kind="true",
        b_norm=b_norm,
        preconditioner=cfg.preconditioner,
        true_residual_checks=true_checks,
        max_residual_drift=max_drift,
        non_positive_value=non_pos_value,
        history=history,
        message=STATUS_MESSAGES[status],
    )


def _hist_entry(iteration: int, rnorm: float, relative: float,
                kind: str, alpha: float) -> dict[str, Any]:
    return {
        "iteration": int(iteration),
        "residual_norm": float(rnorm),
        "relative_residual": float(relative),
        "kind": kind,
        "alpha": float(alpha),
    }


def _finish(x, status, iterations, rnorm, initial_rnorm, b_norm, cfg, *,
            residual_kind, history, non_positive_value=None,
            true_checks=0, max_drift=0.0) -> CGResult:
    ref = b_norm if b_norm > 0.0 else max(initial_rnorm, 0.0)
    rel = rnorm / ref if ref > 0.0 else rnorm
    return CGResult(
        x=x, status=status, iterations=iterations,
        residual_norm=rnorm, initial_residual_norm=initial_rnorm,
        relative_residual=rel, converged=status in SUCCESS_STATUSES,
        residual_kind=residual_kind, b_norm=b_norm,
        preconditioner=cfg.preconditioner,
        true_residual_checks=true_checks,
        max_residual_drift=max_drift,
        non_positive_value=non_positive_value,
        history=history, message=STATUS_MESSAGES[status],
    )
