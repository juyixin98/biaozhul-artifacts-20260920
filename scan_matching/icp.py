"""Point-to-point ICP for 2D scan matching.

Pipeline per iteration:
  1. Transform the source cloud by the current pose estimate.
  2. Nearest-neighbor correspondence search against the target cloud.
  3. Outlier rejection: pairs farther than ``max_correspondence_distance``
     are dropped, then only the best ``trim_ratio`` fraction is kept.
  4. Closed-form SE(2) update via SVD (Kabsch/Umeyama in 2D).
  5. Convergence check on the update magnitude and residual change.

Degeneracy (e.g. all matched points on a straight line) is detected from
the covariance of the final matched points: when the eigenvalue ratio
lambda_min / lambda_max falls below ``degeneracy_ratio_threshold`` the
points are effectively collinear, translation along the line is
unobservable, and the result is flagged ``degenerate=True`` with the
unconstrained direction reported. Such results must be treated as
uncertain.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from scan_matching.geometry import SE2Pose, apply_transform, compose

# Status values returned in ICPResult.status
STATUS_CONVERGED = "converged"
STATUS_MAX_ITERATIONS = "max_iterations"
STATUS_FAILED = "failed"

# Below this many correspondences a closed-form update is meaningless.
_MIN_CORRESPONDENCES = 3

# Chunk size for the brute-force nearest-neighbor search, bounding memory.
_NN_CHUNK_SIZE = 1024


@dataclass(frozen=True)
class ICPParams:
    """Tunable parameters of the ICP loop."""

    max_iterations: int = 50
    translation_tolerance: float = 1e-5
    rotation_tolerance: float = 1e-5
    residual_tolerance: float = 1e-8
    max_correspondence_distance: float = 2.0
    trim_ratio: float = 1.0
    degeneracy_ratio_threshold: float = 1e-3

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be >= 1")
        if not 0.0 < self.trim_ratio <= 1.0:
            raise ValueError("trim_ratio must be in (0, 1]")
        if self.max_correspondence_distance <= 0.0:
            raise ValueError("max_correspondence_distance must be > 0")

    @staticmethod
    def from_dict(d: dict | None) -> "ICPParams":
        if not d:
            return ICPParams()
        allowed = {f for f in ICPParams.__dataclass_fields__}
        unknown = set(d) - allowed
        if unknown:
            raise ValueError(f"unknown ICP parameter(s): {sorted(unknown)}")
        return ICPParams(**{k: float(v) if k != "max_iterations" else int(v)
                            for k, v in d.items()})


@dataclass
class IterationRecord:
    """Diagnostics for one ICP iteration."""

    iteration: int
    residual: float
    num_correspondences: int
    delta_translation: float
    delta_rotation: float

    def to_dict(self) -> dict:
        return {
            "iteration": self.iteration,
            "residual": self.residual,
            "num_correspondences": self.num_correspondences,
            "delta_translation": self.delta_translation,
            "delta_rotation": self.delta_rotation,
        }


@dataclass
class ICPResult:
    """Outcome of an ICP run."""

    pose: SE2Pose
    status: str
    converged: bool
    degenerate: bool
    residual: float
    iterations: list = field(default_factory=list)
    num_correspondences: int = 0
    degenerate_direction: list | None = None
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "pose": self.pose.to_dict(),
            "status": self.status,
            "converged": self.converged,
            "degenerate": self.degenerate,
            "degenerate_direction": self.degenerate_direction,
            # Non-finite residuals (early failure) serialize as null so the
            # output stays valid strict JSON.
            "residual": (self.residual
                         if math.isfinite(self.residual) else None),
            "num_correspondences": self.num_correspondences,
            "iterations": [r.to_dict() for r in self.iterations],
            "message": self.message,
        }


def nearest_neighbors(source: np.ndarray,
                      target: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Brute-force nearest neighbor in ``target`` for each row of ``source``.

    Returns (indices, distances), each of shape (len(source),). The source
    is processed in chunks; peak memory is O(chunk_size * len(target)).
    """
    indices = np.empty(len(source), dtype=np.int64)
    distances = np.empty(len(source), dtype=float)
    for start in range(0, len(source), _NN_CHUNK_SIZE):
        chunk = source[start:start + _NN_CHUNK_SIZE]
        # (C, 1, 2) - (M, 2) -> (C, M) squared distances
        diff = chunk[:, None, :] - target[None, :, :]
        sq_dist = np.einsum("cmd,cmd->cm", diff, diff)
        idx = np.argmin(sq_dist, axis=1)
        indices[start:start + len(chunk)] = idx
        distances[start:start + len(chunk)] = np.sqrt(
            sq_dist[np.arange(len(chunk)), idx])
    return indices, distances


def reject_outliers(distances: np.ndarray,
                    max_distance: float,
                    trim_ratio: float) -> np.ndarray:
    """Return the sorted indices of pairs kept after outlier rejection.

    First drops pairs beyond ``max_distance``, then keeps only the best
    ``trim_ratio`` fraction of the survivors (smallest distances). At
    least ``_MIN_CORRESPONDENCES`` pairs are kept so the closed-form
    update stays defined.
    """
    within = np.flatnonzero(distances <= max_distance)
    if within.size == 0:
        return within
    keep_count = max(_MIN_CORRESPONDENCES,
                     int(math.ceil(trim_ratio * within.size)))
    keep_count = min(keep_count, within.size)
    order = np.argsort(distances[within], kind="stable")[:keep_count]
    return np.sort(within[order])


def estimate_rigid_transform(source: np.ndarray,
                             target: np.ndarray) -> SE2Pose:
    """Closed-form SE(2) transform aligning ``source`` onto ``target``.

    Both arrays are (N, 2) with pairwise correspondence by row. Solved with
    SVD (Kabsch); the rotation is guaranteed proper (det = +1).
    """
    src_centroid = source.mean(axis=0)
    tgt_centroid = target.mean(axis=0)
    src_centered = source - src_centroid
    tgt_centered = target - tgt_centroid
    # Cross-covariance W = sum s_i t_i^T; with W = U S V^T the optimal
    # rotation is R = V U^T (Kabsch), corrected to stay proper (det = +1).
    w = src_centered.T @ tgt_centered
    u, _, vt = np.linalg.svd(w)
    det_fix = np.diag([1.0, np.linalg.det(vt.T @ u.T)])
    rotation = vt.T @ det_fix @ u.T
    translation = tgt_centroid - rotation @ src_centroid
    return SE2Pose(x=float(translation[0]),
                   y=float(translation[1]),
                   theta=math.atan2(rotation[1, 0], rotation[0, 0]))


def _detect_degeneracy(matched: np.ndarray,
                       threshold: float) -> tuple[bool, list | None]:
    """Detect collinear (rank-deficient) matched-point geometry.

    Uses the eigenvalue ratio of the matched-point covariance. When the
    points collapse onto a line, translation along that line is
    unobservable; the line direction (eigenvector of the largest
    eigenvalue) is reported as the unconstrained direction.
    """
    covariance = np.cov(matched.T)
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    if eigenvalues[1] <= 0.0:
        return True, None
    if eigenvalues[0] / eigenvalues[1] >= threshold:
        return False, None
    direction = eigenvectors[:, 1]
    return True, [float(direction[0]), float(direction[1])]


def _validate_points(name: str, points: np.ndarray) -> np.ndarray:
    arr = np.asarray(points, dtype=float)
    if arr.ndim != 2 or arr.shape[1] != 2:
        raise ValueError(f"{name} must be an (N, 2) array, got shape {arr.shape}")
    if arr.shape[0] < _MIN_CORRESPONDENCES:
        raise ValueError(f"{name} needs at least {_MIN_CORRESPONDENCES} points")
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} contains non-finite coordinates")
    return arr


def icp(source: np.ndarray,
        target: np.ndarray,
        initial_pose: SE2Pose | None = None,
        params: ICPParams | None = None) -> ICPResult:
    """Run point-to-point ICP to align ``source`` onto ``target``.

    Args:
        source: (N, 2) moving point cloud (the new scan).
        target: (M, 2) reference point cloud (the map or previous scan).
        initial_pose: starting SE(2) guess; identity when omitted.
        params: ICPParams; defaults when omitted.

    Returns:
        ICPResult with the estimated pose, per-iteration residuals,
        convergence status and a degeneracy flag.
    """
    source = _validate_points("source", source)
    target = _validate_points("target", target)
    params = params or ICPParams()
    pose = initial_pose or SE2Pose()

    iterations: list[IterationRecord] = []
    residual = math.inf
    num_corr = 0
    status = STATUS_MAX_ITERATIONS
    message = "reached max_iterations without converging"

    for iteration in range(params.max_iterations):
        transformed = apply_transform(source, pose)
        nn_idx, nn_dist = nearest_neighbors(transformed, target)
        keep = reject_outliers(nn_dist,
                               params.max_correspondence_distance,
                               params.trim_ratio)
        num_corr = int(keep.size)
        if num_corr < _MIN_CORRESPONDENCES:
            return ICPResult(
                pose=pose, status=STATUS_FAILED, converged=False,
                degenerate=False, residual=residual, iterations=iterations,
                num_correspondences=num_corr,
                message=(f"only {num_corr} correspondences within "
                         f"{params.max_correspondence_distance} m; "
                         "cannot estimate a pose"))

        matched_src = transformed[keep]
        matched_tgt = target[nn_idx[keep]]
        residual = float(np.sqrt(np.mean(
            np.sum((matched_src - matched_tgt) ** 2, axis=1))))

        delta = estimate_rigid_transform(matched_src, matched_tgt)
        pose = compose(delta, pose)

        delta_translation = math.hypot(delta.x, delta.y)
        delta_rotation = abs(delta.theta)
        iterations.append(IterationRecord(
            iteration=iteration,
            residual=residual,
            num_correspondences=num_corr,
            delta_translation=delta_translation,
            delta_rotation=delta_rotation))

        step_converged = (delta_translation < params.translation_tolerance
                          and delta_rotation < params.rotation_tolerance)
        # Only a *decreasing* residual that has stalled counts as converged;
        # an increasing residual must not declare convergence.
        residual_stalled = (len(iterations) > 1
                            and 0.0 <= iterations[-2].residual - residual
                            < params.residual_tolerance)
        if step_converged or residual_stalled:
            status = STATUS_CONVERGED
            message = "converged"
            break

    # Recompute correspondences, residual and degeneracy at the final pose
    # so the reported diagnostics describe the returned pose itself.
    transformed = apply_transform(source, pose)
    nn_idx, nn_dist = nearest_neighbors(transformed, target)
    keep = reject_outliers(nn_dist,
                           params.max_correspondence_distance,
                           params.trim_ratio)
    num_corr = int(keep.size)
    if keep.size >= _MIN_CORRESPONDENCES:
        matched = transformed[keep]
        residual = float(np.sqrt(np.mean(
            np.sum((matched - target[nn_idx[keep]]) ** 2, axis=1))))
        degenerate, direction = _detect_degeneracy(
            matched, params.degeneracy_ratio_threshold)
    else:
        degenerate, direction = True, None
    if degenerate:
        message += "; degenerate geometry: pose is uncertain " \
                   "along the reported direction"

    return ICPResult(
        pose=pose,
        status=status,
        converged=(status == STATUS_CONVERGED),
        degenerate=degenerate,
        degenerate_direction=direction,
        residual=residual,
        iterations=iterations,
        num_correspondences=num_corr,
        message=message)
