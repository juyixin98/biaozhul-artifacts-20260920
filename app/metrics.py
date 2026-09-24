"""ATE and RPE metric computation over matched, aligned pose pairs."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import EvaluationError
from .geometry import relative_pose, rotation_angle


def _rmse(v: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(v))))


@dataclass(frozen=True)
class ATEResult:
    n: int
    translation_rmse: float
    translation_mean: float
    translation_median: float
    rotation_rmse_deg: float
    translation_errors: np.ndarray  # metres, per pair
    rotation_errors_deg: np.ndarray  # degrees, per pair


@dataclass(frozen=True)
class RPEResult:
    delta_index: int
    tolerance_index: int
    n_pairs: int
    translation_rmse: float
    translation_mean: float
    rotation_rmse_deg: float
    rotation_mean_deg: float
    translation_errors: np.ndarray
    rotation_errors_deg: np.ndarray
    pair_indices: np.ndarray  # (i, j) indices into the matched-pair sequence


def compute_ate(
    est_pos: np.ndarray,
    est_rot: np.ndarray,
    gt_pos: np.ndarray,
    gt_rot: np.ndarray,
) -> ATEResult:
    """Absolute trajectory error on already aligned matched poses.

    Translational error is the Euclidean distance of the positions; rotational
    error is the geodesic angle of the relative rotation.
    """
    n = est_pos.shape[0]
    trans_err = np.linalg.norm(gt_pos - est_pos, axis=1)
    rot_err = np.array(
        [rotation_angle(est_rot[i].T @ gt_rot[i]) for i in range(n)], dtype=np.float64
    )
    rot_err_deg = np.degrees(rot_err)
    return ATEResult(
        n=n,
        translation_rmse=_rmse(trans_err),
        translation_mean=float(np.mean(trans_err)),
        translation_median=float(np.median(trans_err)),
        rotation_rmse_deg=_rmse(rot_err_deg),
        translation_errors=trans_err,
        rotation_errors_deg=rot_err_deg,
    )


def compute_rpe(
    times: np.ndarray,
    est_pos: np.ndarray,
    est_rot: np.ndarray,
    gt_pos: np.ndarray,
    gt_rot: np.ndarray,
    delta_index: int,
    tolerance_index: int,
) -> RPEResult:
    """Relative pose error over index spans of ``delta_index`` matched poses.

    For each matched pose i, the successor j is the matched pose whose index
    offset is within ``delta_index ± tolerance_index`` (nearest offset wins).
    Relative motions are formed in each trajectory's own frame, so the metric
    is independent of global alignment; with scale alignment the estimated
    motion inherits the fitted scale, and the response marks that explicitly.
    """
    n = est_pos.shape[0]
    if delta_index < 1:
        raise EvaluationError(
            "INVALID_REQUEST", "rpe.delta_index must be a positive integer.", {"delta_index": delta_index}
        )
    if tolerance_index < 0:
        raise EvaluationError(
            "INVALID_REQUEST",
            "rpe.tolerance_index must be a non-negative integer.",
            {"tolerance_index": tolerance_index},
        )

    pair_i: list[int] = []
    pair_j: list[int] = []
    for i in range(n):
        best_j = -1
        best_off = None
        for off in range(delta_index - tolerance_index, delta_index + tolerance_index + 1):
            if off < 1:
                continue
            j = i + off
            if j >= n:
                break
            if best_off is None or abs(off - delta_index) < abs(best_off - delta_index) or (
                abs(off - delta_index) == abs(best_off - delta_index) and j < best_j
            ):
                best_j = j
                best_off = off
        if best_j >= 0:
            pair_i.append(i)
            pair_j.append(best_j)

    if not pair_i:
        raise EvaluationError(
            "RPE_NO_VALID_PAIRS",
            "No matched-pose pairs available at the requested RPE index span "
            f"(delta_index={delta_index}); the matched track is too short. "
            "Increase the match coverage or reduce delta_index.",
            {"matched_poses": int(n), "delta_index": delta_index},
        )

    ii = np.asarray(pair_i, dtype=np.int64)
    jj = np.asarray(pair_j, dtype=np.int64)

    trans_err = np.empty(ii.shape[0], dtype=np.float64)
    rot_err = np.empty(ii.shape[0], dtype=np.float64)
    for k, (i, j) in enumerate(zip(ii, jj, strict=True)):
        Re, te = relative_pose(est_rot[i], est_pos[i], est_rot[j], est_pos[j])
        Rg, tg = relative_pose(gt_rot[i], gt_pos[i], gt_rot[j], gt_pos[j])
        # Relative-motion error T_e^{-1} T_g = (Re^T Rg, Re^T (tg - te));
        # the rotation Re^T drops out of the translation norm.
        trans_err[k] = np.linalg.norm(tg - te)
        rot_err[k] = rotation_angle(Re.T @ Rg)

    rot_deg = np.degrees(rot_err)
    return RPEResult(
        delta_index=delta_index,
        tolerance_index=tolerance_index,
        n_pairs=int(ii.shape[0]),
        translation_rmse=_rmse(trans_err),
        translation_mean=float(np.mean(trans_err)),
        rotation_rmse_deg=_rmse(rot_deg),
        rotation_mean_deg=float(np.mean(rot_deg)),
        translation_errors=trans_err,
        rotation_errors_deg=rot_deg,
        pair_indices=np.stack([ii, jj], axis=1),
    )
