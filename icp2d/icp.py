"""Point-to-point ICP for 2D pose estimation.

The solver alternates between:

1. association   -- nearest neighbour in the target scan (KD-tree),
2. rejection     -- explicit outlier gating (see outlier_rejection.py),
3. estimation    -- one Gauss-Newton step on the linearized SE(2) residual
                    r_i = R(theta) p_i + t - q_i,

until the update step and the residual change both fall below tolerance
(status ``converged``) or ``max_iterations`` is reached
(``max_iterations_reached``). If too few correspondences survive rejection,
the solver stops with ``insufficient_inliers`` instead of producing a
meaningless pose.

Every iteration appends the inlier RMSE to ``residual_history`` so callers
can inspect convergence. After the loop, a Gauss-Newton covariance
(sigma^2 * H^-1) and an empirical degeneracy analysis (see degeneracy.py)
are attached; degenerate geometry is reported as ``uncertain`` rather than
silently returning an overconfident pose.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .degeneracy import DegeneracyReport, analyze_degeneracy
from .geometry import compose_poses, rotation_matrix, transform_points
from .kdtree import nearest_neighbors
from .outlier_rejection import reject_outliers

STATUS_CONVERGED = "converged"
STATUS_MAX_ITER = "max_iterations_reached"
STATUS_INSUFFICIENT = "insufficient_inliers"


@dataclass
class ICPConfig:
    """Tunable parameters of the ICP solver."""

    max_iterations: int = 50
    tolerance_translation: float = 1e-4
    tolerance_rotation: float = 1e-5
    tolerance_rmse: float = 1e-6
    max_correspondence_distance: float = 0.5
    rejection_strategy: str = "mad"
    trim_ratio: float = 0.8
    mad_scale: float = 3.0
    min_inliers: int = 6
    min_iterations: int = 2
    degeneracy_flatness_ratio: float = 0.1


@dataclass
class IterationRecord:
    """Book-keeping for a single ICP iteration."""

    iteration: int
    rmse: float
    num_inliers: int
    step_translation: float
    step_rotation: float
    pose: list[float]


@dataclass
class ICPResult:
    """Full result of one ICP run."""

    pose: np.ndarray  # (tx, ty, theta), source frame -> target frame
    status: str
    converged: bool
    iterations: int
    residual_history: list[float]
    final_rmse: float
    num_inliers: int
    num_source_points: int
    covariance: np.ndarray  # 3x3, order (tx, ty, theta)
    hessian_eigenvalues: np.ndarray
    degeneracy: DegeneracyReport
    history: list[IterationRecord] = field(default_factory=list)

    @property
    def uncertain(self) -> bool:
        """True when the estimate should not be trusted at face value."""
        return self.degeneracy.degenerate or self.status == STATUS_INSUFFICIENT


def estimate_pose(
    source: np.ndarray,
    target: np.ndarray,
    initial_pose: np.ndarray | None = None,
    config: ICPConfig | None = None,
) -> ICPResult:
    """Estimate the pose aligning ``source`` onto ``target`` via ICP.

    Args:
        source: (N, 2) points of the moving scan.
        target: (M, 2) points of the reference scan.
        initial_pose: optional (tx, ty, theta) starting guess.
        config: solver configuration; defaults are used when omitted.

    Returns:
        ICPResult with the final pose, per-iteration residuals, convergence
        status, covariance and a degeneracy/uncertainty report.
    """
    config = config or ICPConfig()
    source = np.asarray(source, dtype=float)
    target = np.asarray(target, dtype=float)
    if source.ndim != 2 or source.shape[1] != 2:
        raise ValueError("source must be an (N, 2) array")
    if target.ndim != 2 or target.shape[1] != 2:
        raise ValueError("target must be an (M, 2) array")
    if len(source) < 3 or len(target) < 3:
        raise ValueError("need at least 3 points in both source and target")

    pose = (
        np.zeros(3) if initial_pose is None else np.asarray(initial_pose, dtype=float)
    )

    residual_history: list[float] = []
    records: list[IterationRecord] = []
    status = STATUS_MAX_ITER
    hessian = np.full((3, 3), np.nan)
    num_inliers = 0
    rmse = float("inf")
    prev_rmse = float("inf")

    for iteration in range(1, config.max_iterations + 1):
        transformed = transform_points(source, pose)
        distances, nn_indices = nearest_neighbors(transformed, target)
        rejection = reject_outliers(
            distances,
            strategy=config.rejection_strategy,
            max_distance=config.max_correspondence_distance,
            trim_ratio=config.trim_ratio,
            mad_scale=config.mad_scale,
        )
        mask = rejection.inlier_mask
        num_inliers = int(mask.sum())
        if num_inliers < config.min_inliers:
            status = STATUS_INSUFFICIENT
            break

        src_in = transformed[mask]
        tgt_in = target[nn_indices[mask]]
        residuals = src_in - tgt_in
        rmse = float(np.sqrt(np.mean(np.sum(residuals**2, axis=1))))
        residual_history.append(rmse)

        step, hessian = _gauss_newton_step(source[mask], tgt_in, pose)
        pose = compose_poses(step, pose)
        step_translation = float(np.hypot(step[0], step[1]))
        step_rotation = abs(step[2])
        records.append(
            IterationRecord(
                iteration=iteration,
                rmse=rmse,
                num_inliers=num_inliers,
                step_translation=step_translation,
                step_rotation=step_rotation,
                pose=pose.tolist(),
            )
        )

        if iteration >= config.min_iterations:
            step_small = (
                step_translation < config.tolerance_translation
                and step_rotation < config.tolerance_rotation
            )
            rmse_stalled = abs(prev_rmse - rmse) < config.tolerance_rmse
            if step_small and rmse_stalled:
                status = STATUS_CONVERGED
                break
        prev_rmse = rmse

    covariance, eigenvalues = _covariance_from_hessian(
        hessian, rmse, num_inliers, status
    )
    degeneracy = analyze_degeneracy(
        source=source,
        target=target,
        pose=pose,
        config=config,
        hessian_eigenvalues=eigenvalues,
    )
    return ICPResult(
        pose=pose,
        status=status,
        converged=status == STATUS_CONVERGED,
        iterations=len(records),
        residual_history=residual_history,
        final_rmse=rmse,
        num_inliers=num_inliers,
        num_source_points=len(source),
        covariance=covariance,
        hessian_eigenvalues=eigenvalues,
        degeneracy=degeneracy,
        history=records,
    )


def _gauss_newton_step(
    source_inliers: np.ndarray, target_inliers: np.ndarray, pose: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """One linearized SE(2) least-squares step; returns (step, hessian)."""
    theta = pose[2]
    rotated = source_inliers @ rotation_matrix(theta).T
    residuals = rotated + pose[:2] - target_inliers

    # Jacobian of R(theta) p + t w.r.t. (tx, ty, theta).
    jacobian = np.zeros((len(source_inliers), 2, 3))
    jacobian[:, 0, 0] = 1.0
    jacobian[:, 1, 1] = 1.0
    jacobian[:, 0, 2] = -rotated[:, 1]
    jacobian[:, 1, 2] = rotated[:, 0]
    j2 = jacobian.reshape(2 * len(source_inliers), 3)
    r2 = residuals.reshape(-1)

    hessian = j2.T @ j2
    gradient = j2.T @ r2
    try:
        step = np.linalg.solve(hessian, -gradient)
    except np.linalg.LinAlgError:
        step = np.linalg.lstsq(hessian, -gradient, rcond=None)[0]
    return step, hessian


def _covariance_from_hessian(
    hessian: np.ndarray, rmse: float, num_inliers: int, status: str
) -> tuple[np.ndarray, np.ndarray]:
    """Gauss-Newton covariance sigma^2 * H^-1 (pseudo-inverse when singular)."""
    if status == STATUS_INSUFFICIENT or not np.all(np.isfinite(hessian)):
        return np.full((3, 3), np.nan), np.full(3, np.nan)
    dof = max(2 * num_inliers - 3, 1)
    sigma2 = 2.0 * rmse**2 * num_inliers / dof
    covariance = sigma2 * np.linalg.pinv(hessian)
    eigenvalues = np.linalg.eigvalsh(hessian)[::-1]  # descending
    return covariance, eigenvalues
