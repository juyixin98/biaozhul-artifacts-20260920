"""Acceptance tests for the trajectory error evaluation API.

Scenarios required by the spec:
  1. pure translation (rigid transform) -> ATE ~ 0, RPE ~ 0
  2. scale drift (similarity needed) -> rigid ATE large, similarity recovers,
     and the scale report is explicitly tagged
  3. missing measurements -> partial coverage, no zero filling
  4. rotation across the +/-pi branch cut -> geodesic angle stays small
Plus hard-error cases: duplicate timestamps, zero matches, degenerate
alignment, invalid quaternion.
"""

import copy

import numpy as np

from conftest import circle_poses, qz, transform_poses, zrot_matrix


def _post(client, body):
    return client.post("/evaluate", json=body)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# 1. Pure translation / rigid transform
# ---------------------------------------------------------------------------

def test_pure_translation_rigid(client):
    gt = circle_poses(n=20)
    R = zrot_matrix(0.37)
    est = transform_poses(gt, R=R, t=np.array([1.5, -2.0, 0.7]))
    body = {
        "estimated": est,
        "ground_truth": gt,
        "max_time_diff": 0.01,
        "rpe": [{"delta": 2.0, "tolerance": 0.05}],
    }
    r = _post(client, body)
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["align_mode"] == "rigid"
    assert data["alignment"]["scale"] == {"mode": "fixed_rigid", "value": 1.0}
    assert data["match_count"] == 20
    assert data["coverage"]["estimated"]["ratio"] == 1.0

    assert data["ate"]["trans"]["rmse"] < 1e-9
    assert data["ate"]["rot_deg"]["rmse"] < 1e-6

    rpe = data["rpe"][0]
    assert rpe["num_pairs"] > 0
    assert rpe["trans"]["rmse"] < 1e-9
    assert rpe["rot_deg"]["rmse"] < 1e-6


def test_timestamp_offset_nearest_matching(client):
    """Estimate samples are shifted in time but inside the gate."""
    gt = circle_poses(n=10)
    est = []
    for p in gt:
        q = copy.deepcopy(p)
        q["timestamp"] = p["timestamp"] + 0.02
        est.append(q)
    r = _post(client, {
        "estimated": est, "ground_truth": gt, "max_time_diff": 0.05,
    })
    assert r.status_code == 200
    assert r.json()["match_count"] == 10
    assert abs(r.json()["max_abs_time_error"] - 0.02) < 1e-9


# ---------------------------------------------------------------------------
# 2. Scale drift
# ---------------------------------------------------------------------------

def test_scale_drift_modes_are_separate(client):
    # Estimate was produced at 1.35x GT scale plus an offset.
    gt = circle_poses(n=24, radius=2.0)
    est = transform_poses(gt, s=1.35, t=np.array([0.4, -0.2, 0.1]))

    # Rigid mode: cannot absorb the 35% scale drift -> sizeable ATE.
    r_rigid = _post(client, {
        "estimated": est, "ground_truth": gt, "max_time_diff": 0.01,
        "align_mode": "rigid",
    })
    assert r_rigid.status_code == 200
    rigid = r_rigid.json()
    assert rigid["ate"]["trans"]["rmse"] > 0.1
    assert rigid["alignment"]["scale"]["mode"] == "fixed_rigid"

    # Similarity mode: ATE collapses, fitted scale is recovered and tagged.
    # The transform maps estimate -> GT, so s = 1/1.35.
    r_sim = _post(client, {
        "estimated": est, "ground_truth": gt, "max_time_diff": 0.01,
        "align_mode": "similarity",
        "rpe": [{"delta": 1.0, "tolerance": 0.05}],
    })
    assert r_sim.status_code == 200
    sim = r_sim.json()
    assert sim["ate"]["trans"]["rmse"] < 1e-9
    scale = sim["alignment"]["scale"]
    assert scale["mode"] == "fitted_sim3"
    assert abs(scale["value"] - 1.0 / 1.35) < 1e-8
    assert abs(scale["scale_drift_abs"] - abs(1.0 / 1.35 - 1.0)) < 1e-8
    # constant global scale does not change relative (body-frame) motion
    assert sim["rpe"][0]["trans"]["rmse"] < 1e-9

    # The two reports must not be mixed into one number.
    assert sim["align_mode"] != rigid["align_mode"]


# ---------------------------------------------------------------------------
# 3. Missing measurements
# ---------------------------------------------------------------------------

def test_missing_measurements_partial_coverage(client):
    gt = circle_poses(n=20)
    # Drop every third estimate sample entirely.
    est = [transform_poses([gt[i]], t=np.array([0.1, 0.2, 0.0]))[0]
           for i in range(20) if i % 3 != 0]
    r = _post(client, {
        "estimated": est, "ground_truth": gt, "max_time_diff": 0.01,
        "rpe": [{"delta": 1.0, "tolerance": 0.05}],
    })
    assert r.status_code == 200
    data = r.json()
    assert data["match_count"] == 13
    cov = data["coverage"]
    assert cov["estimated"]["matched"] == 13
    assert cov["estimated"]["ratio"] == 1.0
    assert cov["ground_truth"]["total"] == 20
    assert cov["ground_truth"]["matched_distinct"] == 13
    assert abs(cov["ground_truth"]["ratio"] - 13 / 20) < 1e-12
    assert data["ate"]["num_frames"] == 13  # no zero filling
    # RPE only counts intervals that actually exist within tolerance.
    rpe = data["rpe"][0]
    assert rpe["num_pairs"] > 0
    for pair in rpe["pairs"]:
        assert abs(pair["actual_span"] - 1.0) <= 0.05 + 1e-9


def test_gap_wider_than_rpe_tolerance_yields_zero_pairs(client):
    """Missing data across the requested span -> 0 pairs, not zero error."""
    gt = circle_poses(n=4, dt=1.0)
    r = _post(client, {
        "estimated": gt, "ground_truth": gt, "max_time_diff": 0.01,
        "rpe": [{"delta": 10.0, "tolerance": 0.1}],
    })
    assert r.status_code == 200
    rpe = r.json()["rpe"][0]
    assert rpe["num_pairs"] == 0
    assert rpe["trans"] is None  # explicitly no fabricated statistics
    assert rpe["rot_rad"] is None


# ---------------------------------------------------------------------------
# 4. Rotation across the +/- pi branch cut
# ---------------------------------------------------------------------------

def test_rotation_branch_cut(client):
    """Yaws near +pi and -pi differ by a small physical angle (~10 deg)."""
    n = 12
    poses = []
    for i in range(n):
        t = i * 0.5
        # positions distinct and non-collinear so alignment is well posed
        poses.append({
            "timestamp": t,
            "position": [float(i), 0.5 * np.sin(i), 0.0],
            "orientation": qz(np.pi - 0.1),
        })
    gt_poses = []
    for p in poses:
        q = copy.deepcopy(p)
        q["orientation"] = qz(-np.pi + 0.08)  # naive diff ~ 350 deg
        gt_poses.append(q)

    r = _post(client, {
        "estimated": poses, "ground_truth": gt_poses,
        "max_time_diff": 0.01,
    })
    assert r.status_code == 200
    rot_rmse = r.json()["ate"]["rot_deg"]["rmse"]
    # physical geodesic error ~ 0.18 rad = 10.3 deg, never ~350 deg
    assert 8.0 < rot_rmse < 12.0


def test_rotation_full_turn_recovers(client):
    """Estimate yaw wrapped +2pi relative to GT: geodesic error ~ 0."""
    gt = circle_poses(n=16)
    est = []
    for p in gt:
        q = copy.deepcopy(p)
        a = 2 * np.arctan2(p["orientation"][3], p["orientation"][0])
        q["orientation"] = qz(a + 2 * np.pi)
        est.append(q)
    r = _post(client, {
        "estimated": est, "ground_truth": gt, "max_time_diff": 0.01,
    })
    assert r.status_code == 200
    assert r.json()["ate"]["rot_deg"]["rmse"] < 1e-5


# ---------------------------------------------------------------------------
# Hard errors
# ---------------------------------------------------------------------------

def test_duplicate_timestamp_rejected(client):
    gt = circle_poses(n=5)
    est = circle_poses(n=5)
    est[3]["timestamp"] = est[2]["timestamp"]
    r = _post(client, {
        "estimated": est, "ground_truth": gt, "max_time_diff": 0.01,
    })
    assert r.status_code == 400
    body = r.json()
    assert body["error"] is True
    assert body["code"] == "duplicate_timestamp"


def test_zero_matches_rejected(client):
    gt = circle_poses(n=5, t0=0.0)
    est = circle_poses(n=5, t0=100.0)
    r = _post(client, {
        "estimated": est, "ground_truth": gt, "max_time_diff": 0.1,
    })
    assert r.status_code == 400
    assert r.json()["code"] == "zero_matches"


def test_coincident_points_degenerate(client):
    """All estimate positions identical -> rotation/scale unidentifiable."""
    gt = circle_poses(n=6)
    est = copy.deepcopy(gt)
    for p in est:
        p["position"] = [1.0, 2.0, 3.0]
    r = _post(client, {
        "estimated": est, "ground_truth": gt, "max_time_diff": 0.01,
    })
    assert r.status_code == 400
    assert r.json()["code"] == "degenerate_alignment"


def test_collinear_points_degenerate(client):
    """Points on one line: rotation about that axis is unobservable."""
    n = 5
    gt = [
        {"timestamp": i * 0.5, "position": [float(i), 0.0, 0.0],
         "orientation": [1.0, 0.0, 0.0, 0.0]}
        for i in range(n)
    ]
    est = copy.deepcopy(gt)
    for p in est:
        p["position"] = [p["position"][0] + 0.3, 1.0, 0.0]
    r = _post(client, {
        "estimated": est, "ground_truth": gt, "max_time_diff": 0.01,
    })
    assert r.status_code == 400
    assert r.json()["code"] == "degenerate_alignment"


def test_zero_quaternion_rejected(client):
    gt = circle_poses(n=4)
    est = circle_poses(n=4)
    est[2]["orientation"] = [0.0, 0.0, 0.0, 0.0]
    r = _post(client, {
        "estimated": est, "ground_truth": gt, "max_time_diff": 0.01,
    })
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_orientation"


def test_bad_rpe_delta_rejected(client):
    gt = circle_poses(n=4)
    r = _post(client, {
        "estimated": gt, "ground_truth": gt, "max_time_diff": 0.01,
        "rpe": [{"delta": 0.0, "tolerance": 0.0}],
    })
    assert r.status_code == 422  # pydantic schema validation
