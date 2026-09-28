"""Degenerate-eigenvalue handling: cluster eigenspaces, not eigenvectors.

For a repeated eigenvalue the individual eigenvectors are not canonical - any
orthonormal basis of the eigenspace is correct. Comparing vectors one by one
therefore produces spurious failures. This module groups eigenvalues into
clusters and compares the *subspaces* via principal angles.
"""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class EigenCluster:
    indices: tuple[int, ...]
    value_center: float
    multiplicity: int

    @property
    def is_simple(self) -> bool:
        return self.multiplicity == 1


def cluster_eigenvalues(
    w: np.ndarray, cluster_rtol: float
) -> list[EigenCluster]:
    """Split ascending eigenvalues into clusters by their *relative* gap.

    A new cluster starts whenever the adjacent gap exceeds
    ``cluster_rtol * max(1, |w_{i-1}|, |w_i|)``. The scale is *local* to the
    two eigenvalues rather than the spectrum maximum, so a tiny eigenvalue far
    from a huge one stays identifiable while two genuinely close values
    merge. Near-degenerate but mathematically distinct eigenvalues are
    intentionally clustered when their local relative gap is below the
    tolerance - individual vectors are not identifiable there.
    """
    n = w.shape[0]
    if n == 0:
        return []
    clusters: list[list[int]] = [[0]]
    for i in range(1, n):
        local_scale = max(1.0, float(abs(w[i - 1])), float(abs(w[i])))
        if float(w[i] - w[i - 1]) > cluster_rtol * local_scale:
            clusters.append([i])
        else:
            clusters[-1].append(i)
    return [
        EigenCluster(
            indices=tuple(group),
            value_center=float(np.mean(w[list(group)])),
            multiplicity=len(group),
        )
        for group in clusters
    ]


@dataclass(frozen=True)
class SubspaceComparison:
    multiplicity: int
    our_center: float
    reference_center: float
    eigenvalue_abs_error: float
    max_principal_angle: float
    """Largest principal angle between the two eigenspaces (radians)."""

    max_sin_principal_angle: float
    """sin(theta_max) - the canonical subspace-distance, 0 == identical."""

    max_vector_overlap_error: float
    """For simple eigenvalues: 1 - |v^T q|; 0 for degenerate clusters."""

    @property
    def subspace_matches(self) -> bool:
        return self.max_sin_principal_angle < 1.0


def principal_angles(U: np.ndarray, W: np.ndarray) -> np.ndarray:
    """Principal angles between two orthonormal bases.

    Cosines come from the SVD of ``U^T W``. For angles near zero the
    ``arccos`` / ``sqrt(1 - c^2)`` path suffers catastrophic cancellation
    (a cosine of 1 - 4e-16 spuriously yields sin ~ 3e-8), so the small-angle
    sines are computed stably as the singular values of the projection of
    ``W`` onto the orthogonal complement of ``U``. Both estimates are
    combined, taking the well-conditioned one per angle.
    """
    cosines = np.clip(np.linalg.svd(U.T @ W, compute_uv=False), 0.0, 1.0)
    # Stable principal sines: || (I - U U^T) W || singular values.
    projected = W - U @ (U.T @ W)
    sines = np.clip(np.linalg.svd(projected, compute_uv=False), 0.0, 1.0)
    sines = np.sort(sines)  # ascending, matching descending cosines
    cosines = np.sort(cosines)[::-1]
    angles = np.empty_like(cosines)
    for i, c in enumerate(cosines):
        if c < 0.9:
            angles[i] = np.arccos(c)
        else:
            # small-angle regime: the projection estimate is the accurate one
            angles[i] = np.arcsin(min(1.0, sines[i]))
    return np.sort(angles)


def subspace_sines(U: np.ndarray, W: np.ndarray) -> np.ndarray:
    """Stable principal sines sin(theta_i) via orthogonal-complement
    projection (accurate when the subspaces nearly coincide)."""
    projected = W - U @ (U.T @ W)
    return np.clip(np.linalg.svd(projected, compute_uv=False), 0.0, 1.0)


def compare_clusters(
    w_ours: np.ndarray,
    V_ours: np.ndarray,
    w_ref: np.ndarray,
    V_ref: np.ndarray,
    cluster_rtol: float,
) -> list[SubspaceComparison]:
    """Compare eigenspaces cluster by cluster.

    Cluster boundaries are derived independently from each spectrum; the
    comparison pairs clusters by order after asserting the partitions agree
    (an eigenvalue error large enough to restructure clusters is itself a
    failure, surfaced as ``ValueError``).
    """
    ours = cluster_eigenvalues(w_ours, cluster_rtol)
    ref = cluster_eigenvalues(w_ref, cluster_rtol)
    if len(ours) != len(ref) or [c.multiplicity for c in ours] != [
        c.multiplicity for c in ref
    ]:
        raise ValueError(
            "cluster structure disagrees with reference: "
            f"ours {[c.multiplicity for c in ours]}, "
            f"reference {[c.multiplicity for c in ref]}"
        )

    comparisons: list[SubspaceComparison] = []
    for c_ours, c_ref in zip(ours, ref, strict=True):
        i_ours = list(c_ours.indices)
        i_ref = list(c_ref.indices)
        eig_abs_error = float(
            np.max(np.abs(w_ours[i_ours] - w_ref[i_ref]))
        )
        U = V_ours[:, i_ours]
        W = V_ref[:, i_ref]
        angles = principal_angles(U, W)
        sines = np.sort(subspace_sines(U, W))
        max_sin = float(sines[-1])
        overlap_error = 0.0
        if c_ours.is_simple:
            overlap_error = float(1.0 - np.cos(angles[-1]))
        comparisons.append(
            SubspaceComparison(
                multiplicity=c_ours.multiplicity,
                our_center=c_ours.value_center,
                reference_center=c_ref.value_center,
                eigenvalue_abs_error=eig_abs_error,
                max_principal_angle=float(angles[-1]),
                max_sin_principal_angle=max_sin,
                max_vector_overlap_error=overlap_error,
            )
        )
    return comparisons
