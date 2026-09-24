"""Point-to-point Iterative Closest Point (ICP) backend.

Pure NumPy/SciPy implementation. No hardware, no visualization.

Pipeline per iteration:
  1. Apply the current transform to the source points.
  2. Find nearest neighbours in the target with scipy.spatial.cKDTree.
  3. Reject outliers by a hard correspondence distance and/or a robust
     quantile on the squared distances.
  4. Estimate the incremental rigid transform with the SVD method
     (Arun/Umeyama), projecting reflection solutions back to SO(3).
  5. Detect degeneracy (collinear / planar inlier sets) from the singular
     values of the cross-covariance.

ICP is a LOCAL optimizer: convergence only means the iterates stopped
moving. Whether the solution is the global optimum depends on the initial
guess and on the overlap. This module never claims global optimality; it
reports ``converged``/``max_iterations`` together with warnings that flag
suspected local minima.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

import numpy as np
from scipy.spatial import cKDTree


class ICPStatus(str, Enum):
    """Outcome of an ICP run."""

    CONVERGED = "converged"
    MAX_ITERATIONS = "max_iterations"
    INSUFFICIENT_PAIRS = "insufficient_pairs"


@dataclass
class DegeneracyInfo:
    """Degeneracy of the final rigid estimate, derived from SVD values."""

    degenerate: bool
    kind: str  # "none" | "collinear" | "planar"
    rank: int
    singular_values: list[float]
    ratio: float | None  # smallest / middle singular value
    warning: str | None = None


@dataclass
class ICPResult:
    status: ICPStatus
    R: np.ndarray  # (3, 3) accumulated rotation, world-aligned frame
    t: np.ndarray  # (3,) accumulated translation
    iterations: int
    rmse: float | None  # RMSE over the final inlier correspondences
    inlier_count: int
    inlier_fraction: float
    num_source: int
    num_target: int
    correspondence_stable: bool  # NN associations identical in last 2 iters
    degeneracy: DegeneracyInfo
    reflections_rejected: int
    warnings: list[str] = field(default_factory=list)
    history: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["R"] = np.asarray(self.R).tolist()
        d["t"] = np.asarray(self.t).tolist()
        return d


def validate_rotation(R: Any, tol: float = 1e-4) -> np.ndarray:
    """Validate that ``R`` is a 3x3 rotation matrix (SO(3), not O(3))."""
    arr = np.asarray(R, dtype=float)
    if arr.shape != (3, 3):
        raise ValueError(f"R must have shape (3, 3), got {arr.shape}")
    orth_err = float(np.max(np.abs(arr.T @ arr - np.eye(3))))
    if orth_err > tol:
        raise ValueError(
            f"R is not orthogonal: max|R^T R - I| = {orth_err:.3e} > {tol:.0e}"
        )
    det = float(np.linalg.det(arr))
    if abs(det - 1.0) > tol * 1e2:
        raise ValueError(f"R must have determinant +1, got {det:.6f}")
    return arr


def _rotation_aligning_axes(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Smallest-angle proper rotation mapping unit vector ``a`` onto ``b``."""
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    v = np.cross(a, b)
    c = float(np.dot(a, b))
    if np.linalg.norm(v) < 1e-12:
        # Parallel: identity for same direction, 180-deg rotation for opposite.
        return np.eye(3) if c >= 0.0 else _pi_rotation(a)
    vx = np.array([[0.0, -v[2], v[1]],
                   [v[2], 0.0, -v[0]],
                   [-v[1], v[0], 0.0]])
    return np.eye(3) + vx + vx @ vx / (1.0 + c)


def _pi_rotation(axis: np.ndarray) -> np.ndarray:
    """Proper 180-degree rotation about ``axis``."""
    x, y, z = np.asarray(axis, float)
    return 2.0 * np.outer([x, y, z], [x, y, z]) - np.eye(3)


def estimate_rigid_transform(
    src: np.ndarray, dst: np.ndarray, degeneracy_ratio: float = 1e-3
) -> tuple[np.ndarray, np.ndarray, DegeneracyInfo, bool]:
    """Least-squares rigid transform ``dst ≈ R @ src + t`` (SVD).

    Returns ``(R, t, degeneracy, reflection_rejected)``.

    Reflection rejection: if the raw SVD rotation has determinant -1, the
    Arun/Horn correction is applied (flip the sign of the column of V
    associated with the smallest singular value). The returned R is always
    in SO(3); the caller is told a reflection was forced.

    Rank-1 (collinear) geometry needs extra care: the standard reflection
    correction can pick a 180-degree flip about the fitted line, which is a
    proper rotation but reverses the point ordering. Only the mapping of the
    line *direction* is observable, so in that case the minimum-angle
    rotation aligning the two line directions is returned instead.
    """
    src = np.asarray(src, dtype=float)
    dst = np.asarray(dst, dtype=float)
    if src.shape != dst.shape or src.ndim != 2 or src.shape[1] != 3:
        raise ValueError("src and dst must be (N, 3) arrays of equal length")
    n = src.shape[0]
    if n < 3:
        raise ValueError("at least 3 point pairs are required")

    src_mean = src.mean(axis=0)
    dst_mean = dst.mean(axis=0)
    src_c = src - src_mean
    dst_c = dst - dst_mean

    H = dst_c.T @ src_c / n
    U, S, Vt = np.linalg.svd(H)

    R = U @ Vt
    reflection = False
    if np.linalg.det(R) < 0.0:
        reflection = True
        # Det(U)*det(V) = -1: flipping the smallest-singular column gives
        # the closest proper rotation.
        Vt = Vt.copy()
        Vt[-1, :] *= -1.0
        R = U @ Vt

    s = np.asarray(S, dtype=float)
    smax = float(s[0]) if s[0] > 0 else 0.0
    mid_ratio = float(s[1] / smax) if smax > 0 else 0.0
    low_ratio = float(s[2] / smax) if smax > 0 else 0.0

    if mid_ratio < degeneracy_ratio:
        degenerate = True
        kind = "collinear"
        rank = 1
        warning = (
            "collinear point set: only the line direction (and translation "
            "along it up to ordering) is observable; rotation about the line "
            "and end-for-end flip are arbitrary — minimum-angle fit returned"
        )
        # Recover line directions directly from the centered points and pick
        # the sign from the (ordered) correspondences via a robust score.
        _, _, vh_src = np.linalg.svd(src_c, full_matrices=False)
        _, _, vh_dst = np.linalg.svd(dst_c, full_matrices=False)
        a = vh_src[0]
        b = vh_dst[0]
        sa = src_c @ a
        sb = dst_c @ b
        if float(np.dot(sa, sb)) < 0.0:
            b = -b
        R = _rotation_aligning_axes(a, b)
    elif low_ratio < degeneracy_ratio:
        # Rank-2 geometry. Rotation about the in-plane axes is determined;
        # rotation about the plane normal is only identifiable from the
        # in-plane covariance shape, so flag it rather than pretend full rank.
        degenerate = True
        kind = "planar"
        rank = 2
        warning = (
            "planar (rank-2) point set: rotation about the plane normal is "
            "weakly determined; estimate may be unreliable"
        )
    else:
        degenerate = False
        kind = "none"
        rank = 3
        warning = None

    t = dst_mean - R @ src_mean

    info = DegeneracyInfo(
        degenerate=degenerate,
        kind=kind,
        rank=rank,
        singular_values=[float(x) for x in s],
        ratio=low_ratio if kind != "collinear" else mid_ratio,
        warning=warning,
    )
    return R, t, info, reflection


def _mean_nn_spacing(points: np.ndarray) -> float:
    """Mean nearest-neighbour spacing (used as a scale for residual checks)."""
    n = len(points)
    if n < 2:
        return 0.0
    k = 2 if n >= 4 else n
    dist, _ = cKDTree(points).query(points, k=k)
    dist = np.asarray(dist)
    if dist.ndim == 1:
        return 0.0
    return float(np.mean(dist[:, 1:]))


def icp(
    source: np.ndarray,
    target: np.ndarray,
    R0: np.ndarray | None = None,
    t0: np.ndarray | None = None,
    *,
    max_iterations: int = 50,
    tolerance: float = 1e-7,
    max_correspondence_distance: float | None = None,
    robust_quantile: float | None = 0.9,
    min_inliers: int = 3,
    degeneracy_ratio: float = 1e-3,
) -> ICPResult:
    """Run point-to-point ICP.

    Parameters
    ----------
    source, target:
        (N, 3) and (M, 3) point clouds. Source is moved onto target.
    R0, t0:
        Initial guess for the transform mapping source -> target.
    max_correspondence_distance:
        Hard rejection: correspondences farther than this are dropped.
    robust_quantile:
        Soft rejection: only the best ``q`` fraction (by squared distance)
        of the *surviving* correspondences is kept each iteration. ``None``
        disables it. Essential for partial-overlap scans.
    min_inliers:
        Minimum correspondences needed after rejection to estimate a pose.
    tolerance:
        Convergence when the relative change of the inlier RMSE drops below
        this value.
    degeneracy_ratio:
        Singular-value ratio threshold for collinear/planar detection.
    """
    src = np.asarray(source, dtype=float)
    tgt = np.asarray(target, dtype=float)
    if src.ndim != 2 or src.shape[1] != 3:
        raise ValueError("source must be of shape (N, 3)")
    if tgt.ndim != 2 or tgt.shape[1] != 3:
        raise ValueError("target must be of shape (M, 3)")
    if len(src) == 0 or len(tgt) == 0:
        raise ValueError("source and target must be non-empty")
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")
    if robust_quantile is not None and not (0.0 < robust_quantile <= 1.0):
        raise ValueError("robust_quantile must be in (0, 1] or None")
    if min_inliers < 3:
        raise ValueError("min_inliers must be >= 3")

    R_acc = np.eye(3) if R0 is None else validate_rotation(R0)
    t_acc = np.zeros(3) if t0 is None else np.asarray(t0, dtype=float).reshape(3)

    tree = cKDTree(tgt)
    n_src = len(src)
    warnings: list[str] = []
    history: list[float] = []
    reflections = 0
    last_inlier_idx: np.ndarray | None = None
    prev_target_idx: np.ndarray | None = None
    correspondence_stable = False
    prev_rmse: float | None = None
    final_deg = DegeneracyInfo(False, "none", 3, [0.0, 0.0, 0.0], None)
    final_rmse: float | None = None
    final_inliers: np.ndarray = np.arange(0)
    status = ICPStatus.MAX_ITERATIONS

    for iteration in range(1, max_iterations + 1):
        moved = (R_acc @ src.T).T + t_acc
        dist, nn_idx = tree.query(moved, k=1)
        keep = np.ones(n_src, dtype=bool)
        if max_correspondence_distance is not None:
            keep &= dist <= max_correspondence_distance

        if not np.any(keep):
            status = ICPStatus.INSUFFICIENT_PAIRS
            final_inliers = np.arange(0)
            final_rmse = None
            break

        cand_idx = np.flatnonzero(keep)
        cand_d2 = dist[cand_idx] ** 2
        if robust_quantile is not None and robust_quantile < 1.0:
            cutoff = float(np.quantile(cand_d2, robust_quantile))
            chosen = cand_d2 <= cutoff
            cand_idx = cand_idx[chosen]
            cand_d2 = cand_d2[chosen]

        if len(cand_idx) < min_inliers:
            status = ICPStatus.INSUFFICIENT_PAIRS
            final_inliers = cand_idx
            final_rmse = float(np.sqrt(cand_d2.mean())) if len(cand_d2) else None
            warnings.append(
                f"only {len(cand_idx)} inlier correspondence(s) after rejection "
                f"(need >= {min_inliers}); pose not updated"
            )
            break

        inlier_src_idx = cand_idx
        inlier_tgt_idx = nn_idx[cand_idx]
        correspondence_stable = (
            last_inlier_idx is not None
            and len(last_inlier_idx) == len(inlier_src_idx)
            and np.array_equal(last_inlier_idx, inlier_src_idx)
            and np.array_equal(nn_idx[cand_idx], prev_target_idx)
        )
        prev_target_idx = inlier_tgt_idx
        last_inlier_idx = inlier_src_idx.copy()

        dR, dt, deg, reflected = estimate_rigid_transform(
            moved[inlier_src_idx],
            tgt[inlier_tgt_idx],
            degeneracy_ratio=degeneracy_ratio,
        )
        reflections += int(reflected)
        final_deg = deg

        R_acc = dR @ R_acc
        t_acc = dR @ t_acc + dt

        rmse = float(np.sqrt(cand_d2.mean()))
        history.append(rmse)
        final_rmse = rmse
        final_inliers = inlier_src_idx

        if prev_rmse is not None:
            denom = max(prev_rmse, 1e-12)
            if (abs(prev_rmse - rmse) / denom < tolerance
                    or (prev_rmse < 1e-10 and rmse < 1e-10)):
                status = ICPStatus.CONVERGED
                break
        prev_rmse = rmse
    else:
        status = ICPStatus.MAX_ITERATIONS

    if status is ICPStatus.INSUFFICIENT_PAIRS:
        if not warnings or "inlier" not in warnings[-1]:
            warnings.append("no valid inlier correspondences; returning initial guess")
    else:
        if status is ICPStatus.MAX_ITERATIONS:
            warnings.append(
                f"did not converge within {max_iterations} iterations; "
                "returning the last estimate (RMSE may still be decreasing)"
            )
        if final_deg.degenerate:
            warnings.append(final_deg.warning or f"{final_deg.kind} degeneracy")
        if reflections:
            warnings.append(
                f"{reflections} reflection solution(s) from SVD were projected to SO(3)"
            )
        if status is ICPStatus.CONVERGED and final_rmse is not None:
            spacing = _mean_nn_spacing(tgt)
            if spacing > 0 and final_rmse > 0.5 * spacing:
                warnings.append(
                    f"converged but RMSE ({final_rmse:.4g}) exceeds half the mean "
                    f"target nearest-neighbour spacing ({spacing:.4g}): likely a "
                    "local minimum / poor overlap, NOT verified as the global optimum"
                )
            else:
                warnings.append(
                    "ICP convergence is local; global optimality is not guaranteed "
                    "and not asserted"
                )

    return ICPResult(
        status=status,
        R=R_acc,
        t=t_acc,
        iterations=len(history),
        rmse=final_rmse,
        inlier_count=int(len(final_inliers)),
        inlier_fraction=float(len(final_inliers) / n_src),
        num_source=int(n_src),
        num_target=int(len(tgt)),
        correspondence_stable=bool(correspondence_stable),
        degeneracy=final_deg,
        reflections_rejected=reflections,
        warnings=warnings,
        history=history,
    )


def rotation_error_deg(R_est: np.ndarray, R_true: np.ndarray) -> float:
    """Geodesic angle error between two rotations, in degrees."""
    Rd = np.asarray(R_est).T @ np.asarray(R_true)
    cosang = float(np.clip((np.trace(Rd) - 1.0) / 2.0, -1.0, 1.0))
    return float(np.degrees(np.arccos(cosang)))


def translation_error(t_est: np.ndarray, t_true: np.ndarray) -> float:
    return float(np.linalg.norm(np.asarray(t_est) - np.asarray(t_true)))
