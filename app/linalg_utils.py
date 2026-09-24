"""Covariance validation, propagation and PSD bookkeeping.

The audit never silently treats perturbing transforms as independent.
Three correlation policies are supported (see :class:`CorrelationPolicy`):

``independent``
    The caller explicitly states that all edge perturbations are
    uncorrelated.  The assumption is echoed back in every response.

``conservative_rho``
    Correlations are unknown.  The caller supplies ``rho`` (a scalar bound or
    a per-step matrix, each entry in [0, 1]) bounding the cross-correlation
    coefficients.  For a PSD joint covariance, congruence with
    ``[[I,I],[I,-I]]`` proves ``C + C^T <= rho (S_i + S_j)`` in PSD order,
    hence after propagation::

        Sigma += rho * (G_i S_i G_i^T + G_j S_j G_j^T)

    which is a *guaranteed PSD over-bound* of every possible cross term
    consistent with that coefficient bound.

``cross_blocks``
    The caller provides the off-diagonal blocks; the joint covariance is
    assembled and verified positive (semi-)definite as a whole.
"""

from __future__ import annotations

import numpy as np

# Eigenvalues below this are treated as zero when checking (semi-)definiteness;
# small negative values at this scale are float noise, not real negativity.
PSD_EIG_TOL = 1.0e-8
# The assembled joint covariance must not be "more negative" than this fraction
# of its largest eigenvalue.
PSD_REL_TOL = 1.0e-8


def sym(A: np.ndarray) -> np.ndarray:
    return 0.5 * (A + A.T)


def is_psd(A: np.ndarray, tol: float = PSD_EIG_TOL, rel_tol: float = PSD_REL_TOL) -> bool:
    """Symmetric positive semi-definite check (with relative slack)."""
    A = sym(np.asarray(A, dtype=float))
    w = np.linalg.eigvalsh(A)
    scale = max(float(np.max(np.abs(w))), 1.0)
    return bool(w.min() >= -tol - rel_tol * scale)


def min_eigenvalue(A: np.ndarray) -> float:
    A = sym(np.asarray(A, dtype=float))
    return float(np.linalg.eigvalsh(A).min())


def nearest_psd(A: np.ndarray) -> tuple[np.ndarray, float]:
    """Project a symmetric matrix onto the PSD cone (Higham 2002).

    Returns ``(A_psd, shift)`` where ``shift`` is how much the spectrum was
    lifted; ``0.0`` means the input was already PSD.
    """
    A = sym(np.asarray(A, dtype=float))
    w, V = np.linalg.eigh(A)
    w_pos = np.clip(w, 0.0, None)
    shift = float(-w.min()) if w.min() < 0.0 else 0.0
    Apsd = (V * w_pos) @ V.T
    return sym(Apsd), shift


def validate_covariance(
    Sigma: np.ndarray, name: str, *, allow_repair: bool = False
) -> tuple[np.ndarray | None, dict | None]:
    """Validate a 6x6 (or n x n) covariance block.

    Returns ``(Sigma_ok_or_none, evidence_or_none)``.  When ``allow_repair``
    is set the nearest-PSD projection is returned and the evidence describes
    the repair; otherwise an invalid block yields ``None`` and an evidence
    record carrying the offending eigenvalue.
    """
    Sigma = np.asarray(Sigma, dtype=float)
    if Sigma.ndim != 2 or Sigma.shape[0] != Sigma.shape[1]:
        return None, {
            "kind": "covariance_not_square",
            "name": name,
            "shape": list(Sigma.shape),
            "message": f"{name}: covariance must be square, got {Sigma.shape}",
        }
    if not np.all(np.isfinite(Sigma)):
        return None, {
            "kind": "covariance_not_finite",
            "name": name,
            "message": f"{name}: covariance contains NaN/Inf",
        }
    S = sym(Sigma)
    asym = float(np.max(np.abs(Sigma - S)))
    wmin = min_eigenvalue(S)
    scale = max(abs(float(v)) for v in np.linalg.eigvalsh(S)) or 1.0
    if wmin < -PSD_EIG_TOL - PSD_REL_TOL * scale:
        if not allow_repair:
            return None, {
                "kind": "covariance_not_psd",
                "name": name,
                "min_eigenvalue": wmin,
                "asymmetry": asym,
                "message": (
                    f"{name}: covariance is not positive semidefinite "
                    f"(min eigenvalue {wmin:.3e})"
                ),
            }
        Spsd, shift = nearest_psd(S)
        return Spsd, {
            "kind": "covariance_repaired",
            "name": name,
            "min_eigenvalue_before": wmin,
            "spectral_shift": shift,
            "asymmetry": asym,
            "message": f"{name}: covariance projected to PSD (spectrum lifted {shift:.3e})",
        }
    return S, None


def validate_cross_block(
    Cij: np.ndarray, Si: np.ndarray, Sj: np.ndarray
) -> float | None:
    """Validate cross-covariance block Cij against marginals Si, Sj.

    The 2x2 block matrix must be PSD.  Its smallest eigenvalue is returned
    (non-negative => valid); ``None`` is returned for shape/finite errors.
    """
    Cij = np.asarray(Cij, dtype=float)
    if Cij.shape != Si.shape:
        return None
    if not (np.all(np.isfinite(Cij)) and np.all(np.isfinite(Si)) and np.all(np.isfinite(Sj))):
        return float("nan")
    joint = np.block([[sym(Si), Cij], [Cij.T, sym(Sj)]])
    return min_eigenvalue(joint)


def symmix(A: np.ndarray, B: np.ndarray) -> np.ndarray:
    """``A + B`` PSD symmetrisation scaled: ``sqrt(rho)`` handled by caller."""
    return sym(A + B)


def propagate_sum(
    blocks: list[np.ndarray],
    jacobians: list[np.ndarray],
    *,
    policy: str,
    rho: float | np.ndarray = 0.0,
    cross_blocks: dict[tuple[int, int], np.ndarray] | None = None,
) -> tuple[np.ndarray, list[dict]]:
    """Propagate per-step 6x6 covariances through chain Jacobians.

    ``blocks[k]`` is the local covariance (already oriented for the traversal
    step, identity orientation for forward edges); ``jacobians[k]`` maps it
    into the residual tangent frame.

    Returns ``(Sigma_total, notes)``.  For ``conservative_rho`` the result is a
    PSD over-bound.  ``cross_blocks`` uses keys ``(i, j)`` with ``i < j``.
    """
    n = len(blocks)
    notes: list[dict] = []
    A: list[np.ndarray] = []
    for k in range(n):
        G = jacobians[k]
        A.append(sym(G @ blocks[k] @ G.T))
    total = np.zeros_like(A[0]) if A else np.zeros((6, 6))
    for k in range(n):
        total += A[k]

    if policy == "independent":
        return sym(total), notes

    if policy == "conservative_rho":
        if np.isscalar(rho):
            rho_mat = np.full((n, n), float(rho))
        else:
            rho_mat = np.asarray(rho, dtype=float)
        for i in range(n):
            for j in range(i + 1, n):
                rij = float(rho_mat[i, j])
                if rij <= 0.0:
                    continue
                if not (0.0 <= rij <= 1.0 + 1e-12):
                    notes.append(
                        {
                            "kind": "rho_out_of_range",
                            "i": i,
                            "j": j,
                            "rho": rij,
                        }
                    )
                rij = min(max(rij, 0.0), 1.0)
                # PSD over-bound of the unknown cross term: if the joint
                # covariance is PSD with correlation bound rho, then
                # C + C^T <= rho (S_i + S_j) in PSD order; applying the
                # propagation Jacobians preserves the Loewner ordering.
                total = total + rij * (A[i] + A[j])
        notes.append(
            {
                "kind": "conservative_over_bound",
                "policy": "conservative_rho",
                "message": (
                    "Unknown cross-correlations were bounded, not assumed zero; "
                    "result is a PSD conservative over-bound."
                ),
            }
        )
        return sym(total), notes

    if policy == "cross_blocks":
        cross_blocks = cross_blocks or {}
        for (i, j), Cij in cross_blocks.items():
            if i >= j or not (0 <= i < n and 0 <= j < n):
                notes.append({"kind": "bad_cross_key", "key": [i, j]})
                continue
            Gi, Gj = jacobians[i], jacobians[j]
            C = np.asarray(Cij, dtype=float)
            # G_i C G_j^T + G_j C^T G_i^T is already symmetric; do not apply
            # sym() here (it would halve the cross contribution).
            total = total + Gi @ C @ Gj.T + Gj @ C.T @ Gi.T
        return sym(total), notes

    raise ValueError(f"unknown correlation policy: {policy!r}")
