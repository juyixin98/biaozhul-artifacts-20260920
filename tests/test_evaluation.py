"""End-to-end metric tests: pure translation, scale drift, missing data,
rotation representation cross-over, and explicit failure paths."""

from __future__ import annotations

import numpy as np
import pytest

from app.errors import EvaluationError
from app.geometry import quat_to_rot
from app.pipeline import evaluate
from app.schemas import EvaluateRequest
from tests._helpers import pose, q_from_axis_angle, q_identity, transform_pose

# A planar rectangular ground-truth path (rank 2 -> rigid alignment valid,
# unlike a collinear path, which must be rejected as degenerate).
_GT_POS = np.array(
    [
        [0.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
        [2.0, 0.0, 0.0],
        [3.0, 0.0, 0.0],
        [3.0, 1.0, 0.0],
        [3.0, 2.0, 0.0],
        [3.0, 3.0, 0.0],
        [2.0, 3.0, 0.0],
        [1.0, 3.0, 0.0],
        [0.0, 3.0, 0.0],
        [0.0, 2.0, 0.0],
        [0.0, 1.0, 0.0],
    ],
    dtype=np.float64,
)
_TIMES = np.round(np.arange(12, dtype=np.float64) * 0.1, 3)
_YAW = np.deg2rad(
    np.array([0, 0, 0, 90, 90, 90, 180, 180, 180, -90, -90, -90], dtype=float)
)
_GT_QUAT = [q_from_axis_angle(np.array([0.0, 0.0, 1.0]), a) for a in _YAW]


def _gt_poses():
    return [pose(t, p, q) for t, p, q in zip(_TIMES, _GT_POS, _GT_QUAT, strict=True)]


def _transformed_gt(R, t, s=1.0, times=None, flip_sign=False):
    out = []
    for i, (tstamp, p, q) in enumerate(zip(_TIMES, _GT_POS, _GT_QUAT, strict=True)):
        p2, q2 = transform_pose(p, q, R, t, s)
        if flip_sign and i % 2 == 1:
            q2 = -q2  # same rotation, opposite quaternion sign
        out.append(pose(times[i] if times is not None else tstamp, p2, q2))
    return out


def _req(est, gt=None, mode="rigid", max_dt=0.02, delta=1, tol=0):
    return EvaluateRequest.model_validate(
        {
            "estimated": est,
            "ground_truth": gt if gt is not None else _gt_poses(),
            "association": {"max_time_diff": max_dt},
            "alignment": {"mode": mode},
            "rpe": {"delta_index": delta, "tolerance_index": tol},
        }
    )


def test_pure_translation_recovers_zero_error():
    R = quat_to_rot(q_from_axis_angle(np.array([0.0, 0.0, 1.0]), 0.6))
    t = np.array([4.0, -2.5, 1.25])
    # Estimated timestamps slightly offset; still within the match window.
    est = _transformed_gt(R, t, times=_TIMES + 0.005)
    res = evaluate(_req(est, max_dt=0.02))

    assert res["scale_alignment_applied"] is False
    assert res["alignment"]["mode"] == "rigid"
    assert res["match"]["n_matched"] == 12
    assert res["match"]["coverage"] == 1.0
    assert res["ate"]["translation_rmse"] < 1e-9
    assert res["ate"]["rotation_rmse_deg"] < 1e-7
    assert res["rpe"]["translation_rmse"] < 1e-9
    assert res["rpe"]["rotation_rmse_deg"] < 1e-7
    assert res["rpe"]["n_pairs"] == 11
    assert abs(res["alignment"]["scale"] - 1.0) < 1e-12


def test_scale_drift_rejected_by_rigid_then_recovered_by_similarity():
    scale = 1.3
    R = quat_to_rot(q_from_axis_angle(np.array([0.0, 1.0, 0.0]), -0.4))
    t = np.array([-1.0, 2.0, 0.5])
    est = _transformed_gt(R, t, s=scale)

    rigid = evaluate(_req(est, mode="rigid"))
    assert rigid["scale_alignment_applied"] is False
    # With the scale mismatch left in, the residual must NOT be reported as 0.
    assert rigid["ate"]["translation_rmse"] > 1e-3

    sim = evaluate(_req(est, mode="similarity"))
    assert sim["scale_alignment_applied"] is True
    # Alignment maps estimated -> ground truth, so the fitted scale is 1/1.3.
    assert abs(sim["alignment"]["scale"] - 1.0 / scale) < 1e-9
    assert sim["ate"]["translation_rmse"] < 1e-8
    assert sim["ate"]["rotation_rmse_deg"] < 1e-7
    assert sim["rpe"]["translation_rmse"] < 1e-8
    assert sim["rpe"]["rotation_rmse_deg"] < 1e-7
    # The two modes' metrics live under the same names but are never mixed:
    # the boolean flag distinguishes them and the rigid result is not mutated.
    assert rigid["alignment"]["scale"] == 1.0


def test_missing_measurements_coverage_and_no_padding():
    R = quat_to_rot(q_from_axis_angle(np.array([0.0, 0.0, 1.0]), 0.2))
    t = np.array([0.5, 0.5, 0.0])
    full = _transformed_gt(R, t, times=_TIMES + 0.008)
    # Drop every other estimate: six ground-truth poses have no partner.
    est = full[::2]
    res = evaluate(_req(est, max_dt=0.02, delta=1))

    assert res["match"]["n_matched"] == 6
    assert res["match"]["coverage_estimated"] == 1.0
    assert res["match"]["coverage_ground_truth"] == pytest.approx(0.5)
    assert res["match"]["coverage"] == pytest.approx(0.5)
    assert res["ate"]["n"] == 6
    assert len(res["ate"]["translation_errors"]) == 6  # no zero padding
    assert res["ate"]["translation_rmse"] < 1e-9
    assert res["rpe"]["n_pairs"] == 5
    assert res["rpe"]["translation_rmse"] < 1e-9
    # Matched pairs are 0.2 s apart despite the nominal 0.1 s sampling.
    assert all(p["time_span_s"] == pytest.approx(0.2) for p in res["rpe"]["pairs"])


def test_rotation_sign_crossover_does_not_inflate_error():
    # Global rotation of 170 deg; every other estimated quaternion is written
    # with the opposite sign (the +/-1 double-cover seam near 180 deg).
    ang = np.deg2rad(170.0)
    R = quat_to_rot(q_from_axis_angle(np.array([0.0, 1.0, 0.0]), ang))
    t = np.array([1.0, -1.0, 0.2])
    est = _transformed_gt(R, t, flip_sign=True)
    res = evaluate(_req(est, max_dt=0.02))

    assert res["ate"]["translation_rmse"] < 1e-9
    # A naive quaternion subtraction would see ~340 deg here; geodesic
    # distance on SO(3) after sign canonicalisation sees zero.
    assert res["ate"]["rotation_rmse_deg"] < 1e-7
    assert res["rpe"]["rotation_rmse_deg"] < 1e-7


def test_rpe_rotation_error_is_geodesic_angle():
    # One segment of the estimate carries an extra 30-degree yaw error.
    est_poses = _transformed_gt(np.eye(3), np.zeros(3))
    p = est_poses[1]
    qbad = q_from_axis_angle(np.array([0.0, 0.0, 1.0]), np.deg2rad(30.0))
    est_poses[1] = pose(p["time"], p["position"], qbad)
    res = evaluate(_req(est_poses, delta=1))
    # Exactly two relative motions touch pose 1: pairs (0,1) and (1,2).
    assert sorted(round(x, 8) for x in res["rpe"]["rotation_errors_deg"]).count(30.0) >= 2
    assert res["rpe"]["rotation_rmse_deg"] > 2.0


def test_duplicate_timestamp_pipeline_error():
    bad = _transformed_gt(np.eye(3), np.zeros(3))
    bad[3] = pose(bad[2]["time"], bad[3]["position"], bad[3]["quaternion_xyzw"])
    with pytest.raises(EvaluationError) as exc:
        evaluate(_req(bad))
    assert exc.value.code == "DUPLICATE_TIMESTAMP"


def test_zero_matches_pipeline_error():
    est = _transformed_gt(np.eye(3), np.zeros(3), times=_TIMES + 50.0)
    with pytest.raises(EvaluationError) as exc:
        evaluate(_req(est, max_dt=0.02))
    assert exc.value.code == "NO_MATCHES"


def test_collinear_track_is_degenerate():
    n = 8
    gt = [pose(float(i), [float(i), 0.0, 0.0], q_identity()) for i in range(n)]
    est = [pose(float(i) + 0.001, [float(i), 0.5, 0.0], q_identity()) for i in range(n)]
    with pytest.raises(EvaluationError) as exc:
        evaluate(_req(est, gt=gt))
    assert exc.value.code == "ALIGNMENT_DEGENERATE"


def test_rpe_span_longer_than_track_errors():
    est = _transformed_gt(np.eye(3), np.zeros(3), times=_TIMES + 0.005)
    # With only one match (short matched sequence) no RPE pair can be formed.
    est_short = est[:1]
    with pytest.raises(EvaluationError) as exc:
        evaluate(_req(est_short, max_dt=0.02, delta=1))
    assert exc.value.code == "ALIGNMENT_DEGENERATE"


def test_rpe_no_pairs_at_requested_span():
    # Four widely spaced matched poses (planar -> alignment well-posed),
    # but delta_index=10 reaches no successor: RPE must fail explicitly
    # rather than silently returning an empty/zero error series.
    times = [0.0, 10.0, 20.0, 30.0]
    gt = [
        pose(t, p, q)
        for t, p, q in zip(
            times,
            [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]],
            [q_identity()] * 4,
            strict=True,
        )
    ]
    est_pts = [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]]
    est = [pose(t + 0.001, p, q_identity()) for t, p in zip(times, est_pts, strict=True)]
    with pytest.raises(EvaluationError) as exc:
        evaluate(_req(est, gt=gt, max_dt=0.02, delta=10))
    assert exc.value.code == "RPE_NO_VALID_PAIRS"
    assert exc.value.details["delta_index"] == 10
