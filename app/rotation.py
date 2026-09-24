"""SO(3) rotation averaging with geodesic residuals and robust (IRLS) weights.

Quaternion convention: (w, x, y, z), unit norm. q and -q represent the same
rotation; all inputs are aligned into one hemisphere before use.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

_EPS = 1e-15


# ---------------------------------------------------------------------------
# Quaternion primitives
# ---------------------------------------------------------------------------

def normalize_quaternion(q: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(q)
    if n < _EPS:
        raise ValueError("zero-norm quaternion")
    return q / n


def quat_multiply(q1: np.ndarray, q2: np.ndarray) -> np.ndarray:
    """Hamilton product q1 ⊗ q2, both (w, x, y, z)."""
    w1, x1, y1, z1 = q1
    w2, x2, y2, z2 = q2
    return np.array([
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
    ])


def quat_conjugate(q: np.ndarray) -> np.ndarray:
    return np.array([q[0], -q[1], -q[2], -q[3]])


def quat_to_rotvec(q: np.ndarray) -> np.ndarray:
    """Log map: unit quaternion -> rotation vector in the tangent space.

    The caller is expected to pass a quaternion already in the hemisphere
    w >= 0 (shortest-path representative).
    """
    q = normalize_quaternion(q)
    if q[0] < 0.0:
        q = -q
    vec = q[1:]
    s = np.linalg.norm(vec)
    if s < 1e-8:
        # Small-angle series: angle/2 ≈ s, direction ≈ vec / s.
        return 2.0 * vec
    angle = 2.0 * np.arctan2(s, q[0])
    return vec / s * angle


def rotvec_to_quat(v: np.ndarray) -> np.ndarray:
    """Exp map: rotation vector -> unit quaternion with w >= 0."""
    theta = np.linalg.norm(v)
    if theta < 1e-12:
        return np.array([1.0, 0.5 * v[0], 0.5 * v[1], 0.5 * v[2]])
    axis = v / theta
    half = 0.5 * theta
    q = np.concatenate([[np.cos(half)], axis * np.sin(half)])
    return normalize_quaternion(q)


def quat_to_matrix(q: np.ndarray) -> np.ndarray:
    q = normalize_quaternion(q)
    w, x, y, z = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def geodesic_angle(q1: np.ndarray, q2: np.ndarray) -> float:
    """Geodesic distance on SO(3) in radians, invariant to q -> -q."""
    d = abs(float(np.dot(normalize_quaternion(q1), normalize_quaternion(q2))))
    return 2.0 * np.arccos(min(1.0, d))


def align_hemisphere(quats: np.ndarray, reference: np.ndarray) -> np.ndarray:
    """Flip signs so every quaternion has non-negative dot with `reference`."""
    dots = quats @ reference
    signs = np.where(dots < 0.0, -1.0, 1.0)
    return quats * signs[:, None]


# ---------------------------------------------------------------------------
# Averaging
# ---------------------------------------------------------------------------

@dataclass
class AverageResult:
    converged: bool
    iterations: int
    quaternion: np.ndarray
    rotation_matrix: np.ndarray
    residuals_rad: np.ndarray
    robust_weights: np.ndarray
    outlier_indices: list[int]
    multi_solution_hint: bool
    eigen_gap: float
    messages: list[str] = field(default_factory=list)


def _chordal_init(quats: np.ndarray, weights: np.ndarray) -> tuple[np.ndarray, float]:
    """Markley's chordal L2 mean: top eigenvector of M = Σ w_i q_i q_iᵀ.

    Returns (mean_quaternion, relative eigenvalue gap). A small gap means the
    two largest eigenvalues are nearly tied, i.e. the average is ambiguous
    (symmetric multi-solution situation).
    """
    m = np.einsum("i,ij,ik->jk", weights, quats, quats)
    eigvals, eigvecs = np.linalg.eigh(m)  # ascending order
    mean = eigvecs[:, -1]
    if mean[0] < 0.0:
        mean = -mean
    top = eigvals[-1]
    gap = (eigvals[-1] - eigvals[-2]) / top if top > _EPS else 0.0
    return mean, float(gap)


def average_rotations(
    quaternions: np.ndarray,
    weights: np.ndarray | None = None,
    max_iterations: int = 100,
    tolerance: float = 1e-12,
    huber_delta: float = 0.5,
    eigen_gap_threshold: float = 1e-3,
    outlier_weight_threshold: float = 0.5,
) -> AverageResult:
    """Weighted robust average of rotations on SO(3).

    IRLS scheme: chordal L2 initialisation, then iterate on the tangent space
    at the current mean with geodesic residuals reweighted by a Huber kernel.
    """
    quats = np.asarray(quaternions, dtype=float)
    if quats.ndim != 2 or quats.shape[1] != 4:
        raise ValueError("quaternions must have shape (n, 4)")
    n = quats.shape[0]
    if n == 0:
        raise ValueError("at least one quaternion is required")

    norms = np.linalg.norm(quats, axis=1)
    if np.any(norms < _EPS):
        raise ValueError("zero-norm quaternion in input")
    quats = quats / norms[:, None]

    if weights is None:
        w = np.full(n, 1.0 / n)
    else:
        w = np.asarray(weights, dtype=float)
        if w.shape != (n,):
            raise ValueError("weights must have shape (n,)")
        if np.any(w <= 0.0) or not np.all(np.isfinite(w)):
            raise ValueError("weights must be positive and finite")
        w = w / w.sum()

    # Unify q / -q equivalence: align everything to the first quaternion.
    quats = align_hemisphere(quats, quats[0])

    mean, eigen_gap = _chordal_init(quats, w)
    mean = mean if np.dot(mean, quats[0]) >= 0 else -mean

    messages: list[str] = []
    converged = False
    iterations = 0
    robust = w.copy()

    for iterations in range(1, max_iterations + 1):
        # Error quaternions in the mean's frame, shortest-path representatives.
        errs = np.array([quat_multiply(quat_conjugate(mean), q) for q in quats])
        errs = np.where(errs[:, :1] < 0.0, -errs, errs)

        rotvecs = np.array([quat_to_rotvec(e) for e in errs])
        residuals = np.linalg.norm(rotvecs, axis=1)

        # Huber IRLS weights on geodesic residuals.
        with np.errstate(divide="ignore", invalid="ignore"):
            huber = np.where(residuals <= huber_delta, 1.0, huber_delta / np.maximum(residuals, _EPS))
        robust = w * huber
        total = robust.sum()
        if total < _EPS:
            messages.append("degenerate weights; stopped")
            break

        step = (robust[:, None] * rotvecs).sum(axis=0) / total
        mean = normalize_quaternion(quat_multiply(mean, rotvec_to_quat(step)))
        if mean[0] < 0.0:
            mean = -mean

        if np.linalg.norm(step) < tolerance:
            converged = True
            break

    # Final residuals and robust weights at the converged mean.
    residuals = np.array([geodesic_angle(mean, q) for q in quats])
    robust = np.where(residuals <= huber_delta, 1.0, huber_delta / np.maximum(residuals, _EPS))
    outlier_indices = [int(i) for i in np.nonzero(robust < outlier_weight_threshold)[0]]

    multi_solution_hint = eigen_gap < eigen_gap_threshold
    if multi_solution_hint:
        messages.append(
            f"eigen gap {eigen_gap:.3e} below threshold {eigen_gap_threshold:.1e}: "
            "input distribution is near-symmetric, multiple averages may be equally valid"
        )
    if not converged:
        messages.append(f"not converged within {max_iterations} iterations")

    return AverageResult(
        converged=converged,
        iterations=iterations,
        quaternion=mean,
        rotation_matrix=quat_to_matrix(mean),
        residuals_rad=residuals,
        robust_weights=robust,
        outlier_indices=outlier_indices,
        multi_solution_hint=multi_solution_hint,
        eigen_gap=eigen_gap,
        messages=messages,
    )
