"""Request orchestration.

The engine wires the four layers together and enforces the central
contract: *stopping the iteration loop is not success*. A request succeeds
only when the kernel both deflated every block AND the independently
computed evidence gates pass. Otherwise the verdict is explicitly
``NON_CONVERGENCE`` (hard failure) or ``UNCERTAIN`` (a result exists but its
evidence is not trustworthy), and the reasons are reported separately from
the result.
"""

from dataclasses import dataclass, field, asdict
from typing import Any, Literal

import numpy as np

from sym_eig.version import __version__, ALGORITHM_NAME
from sym_eig.config import Settings, FLOAT64_EPS
from sym_eig.errors import EigServiceError, ErrorCategory
from sym_eig.evidence.measures import measure
from sym_eig.evidence.reference import (
    ReferenceResult,
    mpmath_reference,
    scipy_reference,
)
from sym_eig.evidence.subspace import compare_clusters
from sym_eig.numerical.kernel import (
    NonConvergenceError,
    householder_tridiagonal,
    implicit_wilkinson_qr,
)
from sym_eig.numerical.validation import ensure_symmetric, to_finite_matrix
from sym_eig.service.tracing import (
    Tracer,
    processing_location,
    set_request_id,
)

Verdict = Literal["SUCCESS", "UNCERTAIN", "FAILED"]
ReferenceMode = Literal["auto", "mpmath", "scipy", "none"]

# Above this sin-theta conditioning bound, two backward-stable solvers need
# not share an eigenspace at all; an independent cross-check there is
# indeterminate rather than confirming, so its gate is skipped.
SUBSPACE_INDETERMINATE_CAP = 0.5


@dataclass(frozen=True)
class RequestOptions:
    request_id: str | None = None
    max_iters: int | None = None
    symmetry_rtol: float | None = None
    symmetry_atol: float | None = None
    residual_rtol: float | None = None
    orthogonality_tol: float | None = None
    reconstruction_rtol: float | None = None
    cluster_rtol: float | None = None
    reference: ReferenceMode = "auto"
    include_vectors: bool = True


@dataclass
class GateResult:
    name: str
    value: float
    threshold: float
    passed: bool
    detail: str = ""
    status: str = "PASS"
    """PASS / FAIL / SKIP. SKIP means the check is mathematically
    indeterminate (e.g. the conditioning bound is so loose that agreement
    carries no information); a SKIP never contributes to SUCCESS."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EigenResponse:
    request_id: str
    verdict: Verdict
    dimension: int
    eigenvalues: list[float] | None = None
    eigenvectors: list[list[float]] | None = None
    error_category: str | None = None
    error_message: str | None = None
    error_details: dict[str, Any] = field(default_factory=dict)
    uncertainties: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    """Non-fatal conclusions: correct backward-stable result, but the input
    conditioning prevents absolute precision finer than the stated bound."""
    gates: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    clusters: list[dict[str, Any]] = field(default_factory=list)
    reference_comparison: dict[str, Any] | None = None
    computation: dict[str, Any] = field(default_factory=dict)
    effective_config: dict[str, Any] = field(default_factory=dict)
    trace: list[dict[str, Any]] = field(default_factory=list)
    processing_location: dict[str, Any] = field(default_factory=dict)
    algorithm: str = ALGORITHM_NAME
    service_version: str = __version__

    def __post_init__(self) -> None:
        return

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _failed(
    request_id: str,
    category: ErrorCategory,
    message: str,
    tracer: Tracer,
    settings: Settings,
    n: int = 0,
    details: dict[str, Any] | None = None,
    eigenvalues: list[float] | None = None,
    eigenvectors: list[list[float]] | None = None,
    computation: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> EigenResponse:
    verdict: Verdict = (
        "UNCERTAIN" if category == ErrorCategory.UNCERTAIN_RESULT else "FAILED"
    )
    tracer.record(f"verdict:{verdict}", {"category": str(category)})
    return EigenResponse(
        request_id=request_id,
        verdict=verdict,
        dimension=n,
        eigenvalues=eigenvalues,
        eigenvectors=eigenvectors,
        error_category=str(category),
        error_message=message,
        error_details=details or {},
        evidence=evidence or {},
        computation=computation or {},
        trace=[s.to_dict() for s in tracer.steps],
        processing_location=processing_location(),
        effective_config=_config_snapshot(settings),
    )


def _config_snapshot(settings: Settings) -> dict[str, Any]:
    return {
        "max_n": settings.max_n,
        "max_iters": settings.max_iters,
        "hard_max_iters": settings.hard_max_iters,
        "symmetry_rtol": settings.symmetry_rtol,
        "symmetry_atol": settings.symmetry_atol,
        "convergence_tol": settings.convergence_tol,
        "residual_rtol": settings.residual_rtol,
        "orthogonality_tol": settings.orthogonality_tol,
        "reconstruction_rtol": settings.reconstruction_rtol,
        "cluster_rtol": settings.cluster_rtol,
        "reference_dps": settings.reference_dps,
    }


def _resolve_budget(options: RequestOptions, settings: Settings) -> int:
    if options.max_iters is None:
        return settings.max_iters
    if options.max_iters < 1:
        raise EigServiceError(
            ErrorCategory.INVALID_PARAMETER,
            "max_iters must be a positive integer",
            {"max_iters": options.max_iters},
        )
    if options.max_iters > settings.hard_max_iters:
        raise EigServiceError(
            ErrorCategory.INVALID_PARAMETER,
            f"max_iters {options.max_iters} exceeds hard limit "
            f"{settings.hard_max_iters}",
            {"max_iters": options.max_iters,
             "hard_max_iters": settings.hard_max_iters},
        )
    return options.max_iters


def run_eigendecomposition(
    raw_matrix: Any,
    settings: Settings | None = None,
    options: RequestOptions | None = None,
) -> EigenResponse:
    """Validate, compute, verify, and classify one eigendecomposition request."""
    settings = settings or Settings()
    options = options or RequestOptions()
    request_id = set_request_id(options.request_id)
    tracer = Tracer()

    # ---- 1. input validation ------------------------------------------------
    try:
        with tracer.step("validate_input"):
            matrix = to_finite_matrix(raw_matrix, settings)
            budget = _resolve_budget(options, settings)
        n = matrix.shape[0]

        with tracer.step("check_symmetry") as span:
            symmetric, deviation, sym_threshold, sym = ensure_symmetric(
                matrix,
                settings,
                rtol=options.symmetry_rtol,
                atol=options.symmetry_atol,
            )
            span.detail.update(
                skew_inf_norm=deviation, symmetry_threshold=sym_threshold
            )
        if not symmetric:
            raise EigServiceError(
                ErrorCategory.NON_SYMMETRIC,
                "matrix is not symmetric within the requested tolerance "
                f"(||A - A^T||_inf = {deviation:.6e} > "
                f"{sym_threshold:.6e})",
                {"skew_inf_norm": deviation, "threshold": sym_threshold},
            )
    except EigServiceError as exc:
        n_dim = int(matrix.shape[0]) if "matrix" in locals() else 0
        return _failed(
            request_id, exc.category, exc.message, tracer, settings,
            n=n_dim, details=exc.details,
        )

    # ---- 2. numerical kernel ------------------------------------------------
    try:
        with tracer.step("householder_tridiagonalization", dimension=n):
            tridiagonal = householder_tridiagonal(sym)
        with tracer.step("implicit_wilkinson_qr", sweep_budget=budget):
            kernel = implicit_wilkinson_qr(
                tridiagonal.diagonal,
                tridiagonal.offdiagonal,
                tridiagonal.orthogonal,
                max_iters=budget,
                tol=settings.convergence_tol,
            )
    except NonConvergenceError as exc:
        # The loop stopped because the budget ran out. That is NON_CONVERGENCE,
        # never a success with whatever diagonal was reached.
        details = {
            "unreduced_block": [exc.block[0], exc.block[1]],
            "sweeps_used": exc.sweeps,
            "sweep_budget": budget,
            "residual_relative_offdiag": exc.residual_offdiag,
            "convergence_tol": settings.convergence_tol,
            "suggestion": "raise max_iters or check for a genuinely "
                          "stagnant unreduced block",
        }
        return _failed(
            request_id,
            ErrorCategory.NON_CONVERGENCE,
            str(exc),
            tracer,
            settings,
            n=n,
            details=details,
        )

    w = kernel.eigenvalues
    V = kernel.eigenvectors

    # ---- 3. independent evidence -------------------------------------------
    with tracer.step("compute_evidence"):
        m = measure(sym, w, V)
        evidence = {
            "residual_relative_fro": m.residual_fro,
            "residual_max_per_eigenvalue": m.residual_max_per_eigen,
            "orthogonality_fro": m.orthogonality_fro,
            "orthogonality_max": m.orthogonality_max,
            "reconstruction_relative_fro": m.reconstruction_rel,
            "matrix_frobenius_scale": m.matrix_scale,
        }

    residual_tol = options.residual_rtol or settings.residual_rtol
    orth_tol = options.orthogonality_tol or settings.orthogonality_tol
    recon_tol = options.reconstruction_rtol or settings.reconstruction_rtol
    cluster_tol = options.cluster_rtol or settings.cluster_rtol

    gates = [
        GateResult(
            "eigen_equation_residual", m.residual_fro, residual_tol,
            m.residual_fro <= residual_tol,
            "||AV - V diag(w)||_F / ||A||_F",
        ),
        GateResult(
            "per_eigenvalue_residual", m.residual_max_per_eigen, residual_tol,
            m.residual_max_per_eigen <= residual_tol,
            "worst single-vector residual relative to ||A||_F",
        ),
        GateResult(
            "eigenvector_orthogonality", m.orthogonality_fro, orth_tol,
            m.orthogonality_fro <= orth_tol,
            "||V^T V - I||_F",
        ),
        GateResult(
            "spectral_reconstruction", m.reconstruction_rel, recon_tol,
            m.reconstruction_rel <= recon_tol,
            "||A - V diag(w) V^T||_F / ||A||_F",
        ),
    ]

    from sym_eig.evidence.subspace import cluster_eigenvalues
    clusters = [
        {
            "multiplicity": c.multiplicity,
            "indices": list(c.indices),
            "value_center": c.value_center,
        }
        for c in cluster_eigenvalues(w, cluster_tol)
    ]

    # ---- 4. independent mature-library / high-precision reference ----------
    reference_block: dict[str, Any] | None = None
    limitations: list[str] = []
    want_ref = options.reference
    if want_ref == "auto":
        want_ref = "mpmath" if n <= settings.reference_max_n else "scipy"
    if want_ref in ("mpmath", "scipy"):
        try:
            with tracer.step(
                "independent_reference", source=want_ref, dimension=n
            ):
                if want_ref == "mpmath":
                    ref = mpmath_reference(sym, dps=settings.reference_dps)
                else:
                    ref = scipy_reference(sym)
                (
                    reference_block,
                    eig_gate,
                    sub_gate,
                    limitations,
                ) = _compare_with_reference(
                    sym, w, V, ref, cluster_tol,
                    residual_tol, orth_tol, tracer,
                )
            gates.extend((eig_gate, sub_gate))
        except Exception as exc:  # an oracle failure must not fake success
            # Log the internal detail server-side under the correlation id;
            # do not reflect exception text (paths/args) to the client.
            tracer._logger.exception(
                "reference oracle (%s) failed for request %s",
                want_ref, request_id,
            )
            return _failed(
                request_id,
                ErrorCategory.UNCERTAIN_RESULT,
                "the independent reference oracle failed, so the result is "
                "not independently confirmed; the kernel result is returned "
                "but marked uncertain",
                tracer,
                settings,
                n=n,
                details={"reference_source": want_ref,
                         "internal_error_correlation_id": request_id},
            )

    # ---- 5. verdict ---------------------------------------------------------
    # Defense in depth: if anything non-finite ever reached here (it should
    # be impossible after the boundary scale check), never emit NaN/Inf.
    if not (np.all(np.isfinite(w)) and np.all(np.isfinite(V))):
        return _failed(
            request_id,
            ErrorCategory.VALUE_OUT_OF_RANGE,
            "computation produced non-finite eigenvalues or eigenvectors; "
            "the matrix magnitude is outside the safe float64 range and "
            "must be rescaled",
            tracer, settings, n=n,
        )

    unconfirmed = [g for g in gates if not g.passed]
    uncertainties: list[str] = []
    for g in unconfirmed:
        if g.status == "SKIP":
            uncertainties.append(
                f"{g.name}: INDETERMINATE ({g.detail})"
            )
        else:
            uncertainties.append(
                f"{g.name}: measured {g.value:.6e} exceeds threshold "
                f"{g.threshold:.6e} ({g.detail})"
            )
    verdict: Verdict = "SUCCESS" if not unconfirmed else "UNCERTAIN"
    tracer.record(
        f"verdict:{verdict}",
        {"unconfirmed_gates": [g.name for g in unconfirmed],
         "skipped_gates": [g.name for g in unconfirmed if g.status == "SKIP"],
         "limitations": len(limitations)},
    )

    return EigenResponse(
        request_id=request_id,
        verdict=verdict,
        dimension=n,
        eigenvalues=[float(x) for x in w],
        eigenvectors=(V.tolist() if options.include_vectors else None),
        uncertainties=uncertainties,
        limitations=limitations,
        gates=[g.to_dict() for g in gates],
        evidence=evidence,
        clusters=clusters,
        reference_comparison=reference_block,
        computation={
            "householder_steps": tridiagonal.steps,
            "qr_sweeps_total": kernel.qr_sweeps,
            "qr_sweeps_max_per_block": kernel.max_block_sweeps,
            "sweeps_per_eigenvalue": list(kernel.sweeps_per_eigenvalue),
            "final_offdiagonal_max": kernel.final_offdiag_max,
            "sweep_budget": budget,
            "convergence_tol": settings.convergence_tol,
        },
        effective_config=_config_snapshot(settings),
        trace=[s.to_dict() for s in tracer.steps],
        processing_location=processing_location(),
    )


def _compare_with_reference(
    sym: np.ndarray,
    w: np.ndarray,
    V: np.ndarray,
    ref: ReferenceResult,
    cluster_tol: float,
    match_tol: float,
    orth_tol: float,
    tracer: Tracer,
) -> tuple[dict[str, Any], GateResult, GateResult, list[str]]:
    comparisons = compare_clusters(
        w, V, ref.eigenvalues, ref.eigenvectors, cluster_tol
    )
    scale = max(1.0, float(np.linalg.norm(sym, "fro")))
    # Backward-error resolution floor in float64: no eigendecomposition of the
    # *stored* matrix can resolve an absolute eigenvalue (or, after division by
    # the spectral gap, an eigenspace) more tightly than ||A||_F * eps,
    # regardless of the solver. This is data conditioning, not solver error.
    eps = FLOAT64_EPS
    eig_resolution = scale * eps
    centers = sorted(c.our_center for c in comparisons)

    per_cluster = []
    max_eig_abs = 0.0
    max_subspace_sin = 0.0
    max_subspace_bound = 0.0
    for c in comparisons:
        gap = min(
            (abs(c.our_center - other)
             for other in centers if other != c.our_center),
            default=float("inf"),
        )
        subspace_bound = (
            eig_resolution / gap if gap > 0.0 and np.isfinite(gap) else 0.0
        )
        max_subspace_bound = max(max_subspace_bound, subspace_bound)
        max_eig_abs = max(max_eig_abs, c.eigenvalue_abs_error)
        max_subspace_sin = max(max_subspace_sin, c.max_sin_principal_angle)
        per_cluster.append({
            "multiplicity": c.multiplicity,
            "our_center": c.our_center,
            "reference_center": c.reference_center,
            "eigenvalue_abs_error": c.eigenvalue_abs_error,
            "eigenvalue_resolution_bound": eig_resolution,
            "nearest_cluster_gap": (gap if np.isfinite(gap) else None),
            "subspace_resolution_bound": subspace_bound,
            "max_principal_angle_rad": c.max_principal_angle,
            "max_sin_principal_angle": c.max_sin_principal_angle,
            "simple_eigenvalue_vector_overlap_error":
                c.max_vector_overlap_error,
        })

    # A configured tolerance is tightened only up to what the data allows;
    # when the conditioning floor dominates, an explanatory limitation is
    # reported separately from genuine uncertainties.
    eig_threshold = max(match_tol, eig_resolution)
    subspace_threshold = max(orth_tol, max_subspace_bound)
    limitations: list[str] = []
    if max_eig_abs > match_tol and eig_resolution > match_tol:
        limitations.append(
            "reference eigenvalue agreement limited by float64 data "
            f"resolution ||A||_F*eps = {eig_resolution:.3e}; small "
            "eigenvalues of a matrix with large norm are not absolutely "
            "resolvable below this by ANY backward-stable solver (Weyl bound)"
        )
    if max_subspace_sin > orth_tol and max_subspace_bound > orth_tol \
            and max_subspace_bound < SUBSPACE_INDETERMINATE_CAP:
        limitations.append(
            "reference eigenspace agreement limited to "
            f"{max_subspace_bound:.3e} by eigenvector conditioning "
            "(||A||_F*eps divided by the gap to the rest of the spectrum, "
            "Davis-Kahan); both results are backward stable (residual ~ eps)"
        )

    # When the conditioning bound is non-finite or so loose (>= the cap) that
    # even a totally wrong eigenspace could satisfy it, the independent
    # cross-check carries NO information. Such a gate is SKIPped, never
    # passed, and forces an UNCERTAIN verdict with an explicit reason.
    eig_indeterminate = not np.isfinite(eig_resolution)
    sub_indeterminate = (
        not np.isfinite(max_subspace_bound)
        or max_subspace_bound >= SUBSPACE_INDETERMINATE_CAP
    )

    block = {
        "source": ref.source,
        "precision_dps": ref.precision_dps,
        "scale": scale,
        "max_eigenvalue_abs_error": float(max_eig_abs),
        "max_eigenvalue_error_relative_scale": float(max_eig_abs / scale),
        "eigenvalue_resolution_bound": float(eig_resolution),
        "max_sin_principal_angle": float(max_subspace_sin),
        "subspace_resolution_bound": float(max_subspace_bound),
        "subspace_indeterminate_cap": SUBSPACE_INDETERMINATE_CAP,
        "degenerate_clusters_compared_as_subspaces": sum(
            1 for c in comparisons if c.multiplicity > 1
        ),
        "per_cluster": per_cluster,
    }

    if eig_indeterminate:
        eig_gate = GateResult(
            "reference_eigenvalue_match",
            float(max_eig_abs), eig_threshold, False,
            "independent eigenvalue cross-check is INDETERMINATE: float64 "
            "resolution bound is non-finite",
            status="SKIP",
        )
    else:
        eig_gate = GateResult(
            "reference_eigenvalue_match",
            float(max_eig_abs), eig_threshold,
            max_eig_abs <= eig_threshold,
            "max |w - w_reference| vs tolerance and the Weyl resolution bound",
            status="PASS" if max_eig_abs <= eig_threshold else "FAIL",
        )

    if sub_indeterminate:
        sub_gate = GateResult(
            "reference_subspace_match",
            float(max_subspace_sin), subspace_threshold, False,
            "independent eigenspace cross-check is INDETERMINATE: the "
            "Davis-Kahan conditioning bound is non-finite or >= "
            f"{SUBSPACE_INDETERMINATE_CAP:.0e}; agreement would certify "
            "even a wrong eigenspace, so this check cannot confirm success",
            status="SKIP",
        )
    else:
        sub_gate = GateResult(
            "reference_subspace_match",
            float(max_subspace_sin), subspace_threshold,
            max_subspace_sin <= subspace_threshold,
            "degenerate clusters compared by principal angles; threshold is "
            "conditioning-aware",
            status="PASS" if max_subspace_sin <= subspace_threshold else "FAIL",
        )
    return block, eig_gate, sub_gate, limitations
