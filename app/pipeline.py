"""End-to-end evaluation pipeline: parse -> match -> align -> ATE/RPE."""

from __future__ import annotations

from typing import Any

import numpy as np

from .alignment import umeyama
from .matching import associate_tracks, build_track
from .metrics import compute_ate, compute_rpe
from .schemas import EvaluateRequest


def _rot_to_list(R: np.ndarray) -> list[list[float]]:
    return [[float(R[i, j]) for j in range(3)] for i in range(3)]


def evaluate(req: EvaluateRequest) -> dict[str, Any]:
    """Run the full evaluation and return a JSON-serializable result dict."""
    est = build_track(req.estimated, "estimated")
    gt = build_track(req.ground_truth, "ground_truth")

    max_dt = req.association.max_time_diff
    assoc = associate_tracks(est, gt, max_dt)
    n_match = int(assoc.est_idx.shape[0])

    est_p = est.positions[assoc.est_idx]
    gt_p = gt.positions[assoc.gt_idx]
    est_r = est.rotations[assoc.est_idx]
    gt_r = gt.rotations[assoc.gt_idx]

    with_scale = req.alignment.mode == "similarity"
    transform = umeyama(est_p, gt_p, with_scale=with_scale)

    # Apply the global alignment to the matched estimates.
    est_p_aligned = transform.apply(est_p)
    est_r_aligned = np.einsum("ij,njk->nik", transform.rotation, est_r)

    ate = compute_ate(est_p_aligned, est_r_aligned, gt_p, gt_r)
    rpe = compute_rpe(
        assoc.gt_times,
        est_p_aligned,
        est_r_aligned,
        gt_p,
        gt_r,
        delta_index=req.rpe.delta_index,
        tolerance_index=req.rpe.tolerance_index,
    )

    matches = [
        {
            "estimated_time": float(assoc.est_times[k]),
            "ground_truth_time": float(assoc.gt_times[k]),
            "time_diff": float(assoc.time_diffs[k]),
            "translation_error": float(ate.translation_errors[k]),
            "rotation_error_deg": float(ate.rotation_errors_deg[k]),
        }
        for k in range(n_match)
    ]

    rpe_pairs = [
        {
            "i": int(rpe.pair_indices[k, 0]),
            "j": int(rpe.pair_indices[k, 1]),
            "ground_truth_time_start": float(assoc.gt_times[rpe.pair_indices[k, 0]]),
            "ground_truth_time_end": float(assoc.gt_times[rpe.pair_indices[k, 1]]),
            "time_span_s": float(
                assoc.gt_times[rpe.pair_indices[k, 1]] - assoc.gt_times[rpe.pair_indices[k, 0]]
            ),
            "translation_error": float(rpe.translation_errors[k]),
            "rotation_error_deg": float(rpe.rotation_errors_deg[k]),
        }
        for k in range(rpe.n_pairs)
    ]

    return {
        "scale_alignment_applied": with_scale,
        "match": {
            "n_estimated": len(est),
            "n_ground_truth": len(gt),
            "n_matched": n_match,
            "coverage_estimated": n_match / len(est),
            "coverage_ground_truth": n_match / len(gt),
            "coverage": n_match / max(len(est), len(gt)),
            "max_time_diff_s": max_dt,
            "max_abs_time_diff_s": float(assoc.time_diffs.max()),
            "mean_time_diff_s": float(assoc.time_diffs.mean()),
            "matches": matches,
        },
        "alignment": {
            "mode": req.alignment.mode,
            "rotation_matrix": _rot_to_list(transform.rotation),
            "translation": [float(x) for x in transform.translation],
            "scale": float(transform.scale),
        },
        "ate": {
            "n": ate.n,
            "translation_rmse": ate.translation_rmse,
            "translation_mean": ate.translation_mean,
            "translation_median": ate.translation_median,
            "rotation_rmse_deg": ate.rotation_rmse_deg,
            "translation_errors": [float(x) for x in ate.translation_errors],
            "rotation_errors_deg": [float(x) for x in ate.rotation_errors_deg],
        },
        "rpe": {
            "delta_index": rpe.delta_index,
            "tolerance_index": rpe.tolerance_index,
            "n_pairs": rpe.n_pairs,
            "translation_rmse": rpe.translation_rmse,
            "translation_mean": rpe.translation_mean,
            "rotation_rmse_deg": rpe.rotation_rmse_deg,
            "rotation_mean_deg": rpe.rotation_mean_deg,
            "pairs": rpe_pairs,
            "translation_errors": [float(x) for x in rpe.translation_errors],
            "rotation_errors_deg": [float(x) for x in rpe.rotation_errors_deg],
        },
    }
