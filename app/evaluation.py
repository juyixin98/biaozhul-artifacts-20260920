"""End-to-end evaluation pipeline (framework-independent, reusable)."""

from __future__ import annotations

import numpy as np

from .alignment import align_trajectories
from .metrics import compute_ate, compute_rpe, make_aligned_sequence
from .rotation import matrix_to_quat
from .trajectory import associate, build_trajectory


def evaluate(req) -> dict:
    """Run association -> alignment -> metrics for one ``EvalRequest``."""
    est = build_trajectory(
        [p.model_dump() for p in req.estimated], "estimated"
    )
    gt = build_trajectory(
        [p.model_dump() for p in req.ground_truth], "ground_truth"
    )

    pairs = associate(est, gt, req.max_time_diff)
    a = align_trajectories(
        est.positions[pairs.est_idx],
        gt.positions[pairs.gt_idx],
        with_scale=(req.align_mode == "similarity"),
    )
    seq = make_aligned_sequence(est, gt, pairs, a)
    ate = compute_ate(seq)
    rpe_reports = [
        compute_rpe(seq, spec.delta, spec.tolerance) for spec in req.rpe
    ]

    return {
        "align_mode": req.align_mode,
        **_coverage_block(est, gt, pairs),
        "alignment": _alignment_block(a),
        "ate": ate,
        "rpe": rpe_reports,
    }


def _coverage_block(est, gt, pairs) -> dict:
    matched = len(pairs)
    distinct_gt = int(np.unique(pairs.gt_idx).size)
    return {
        "match_count": matched,
        "coverage": {
            "estimated": {
                "total": len(est),
                "matched": matched,
                "ratio": matched / len(est),
            },
            "ground_truth": {
                "total": len(gt),
                "matched_distinct": distinct_gt,
                "ratio": distinct_gt / len(gt),
            },
        },
        "max_abs_time_error": float(np.max(np.abs(pairs.time_errors))),
        "time_errors": [float(x) for x in pairs.time_errors],
    }


def _alignment_block(a) -> dict:
    """Serialize the fitted transform.

    ``scale`` is always present for clarity but its meaning is explicitly
    tagged: 1.0 fixed in rigid mode, fitted in similarity mode. The
    scale-aware report is a separate key so it can never be confused with
    rigid-scale numbers.
    """
    block = {
        "rotation_matrix": [[float(v) for v in row] for row in a.rotation],
        "rotation_quat_wxyz": [float(v) for v in matrix_to_quat(a.rotation)],
        "translation": [float(v) for v in a.translation],
        "transform": (
            "p_aligned = scale * R @ p_estimated + translation; "
            "Q_aligned = R @ Q_estimated"
        ),
    }
    if a.with_scale:
        block["scale"] = {
            "mode": "fitted_sim3",
            "value": a.scale,
            "scale_drift_abs": a.scale_drift,
            "warning": (
                "scale was estimated from the same data scored below; "
                "rigid and similarity metrics are not directly comparable"
            ),
        }
    else:
        block["scale"] = {"mode": "fixed_rigid", "value": 1.0}
    block["rms_position_error_before_alignment"] = a.rms_raw
    return block
