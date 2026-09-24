"""Tests for the real calibration pipeline (no HTTP)."""
from __future__ import annotations

import numpy as np

from tests.conftest import read_images

W, H = 1280, 960
R, C = 6, 9
SQ = 40.0


def test_recovers_ground_truth_intrinsics_and_distortion(fx, data_dir):
    from app.calibration import calibrate_pipeline

    imgs = read_images("valid_views")
    out = calibrate_pipeline(imgs, "camA", W, H, R, C, SQ)

    assert out.success, f"{out.failure_reason}: {out.failure_detail}"
    assert len(out.accepted_indices) == 24
    assert out.rms < 0.5

    K = out.camera_matrix
    Kt = np.array(fx["gt"]["camera_matrix"])
    # focal within 1%, principal point within 4 pixels
    assert abs(K[0, 0] - Kt[0, 0]) / Kt[0, 0] < 0.01
    assert abs(K[1, 1] - Kt[1, 1]) / Kt[1, 1] < 0.01
    assert abs(K[0, 2] - Kt[0, 2]) < 4.0
    assert abs(K[1, 2] - Kt[1, 2]) < 4.0

    D = out.dist_coeffs.reshape(-1)
    Dt = np.array(fx["gt"]["dist_coeffs"])
    assert abs(D[0] - Dt[0]) < 0.04      # k1
    assert abs(D[1] - Dt[1]) < 0.12      # k2 (weakly observable at this FOV)
    assert abs(D[2] - Dt[2]) < 6e-4      # p1
    assert abs(D[3] - Dt[3]) < 6e-4      # p2
    assert D[4] == 0.0                   # k3 fixed by design

    acc = [t for t in out.traces if t.index in set(out.accepted_indices)]
    for t in acc:
        assert t.reprojection_error_px is not None
        assert np.isfinite(t.reprojection_error_px)
        assert t.reprojection_error_px < 1.0
        assert t.rvec is not None and t.tvec is not None


def test_duplicate_views_are_flagged_and_removed(data_dir):
    from app.calibration import calibrate_pipeline

    imgs = read_images("repeated_views")
    out = calibrate_pipeline(imgs, "camA", W, H, R, C, SQ)
    assert out.success, out.failure_detail
    dups = [t for t in out.traces if t.reason == "duplicate_pose"]
    assert len(dups) == 6
    for t in dups:
        assert t.duplicate_of is not None
        assert t.status == "rejected"
        assert "relative rotation" in (t.detail or "")
    assert len(out.accepted_indices) == 24


def test_degenerate_planar_translation_fails(data_dir):
    from app.calibration import calibrate_pipeline

    imgs = read_images("planar_arc")
    out = calibrate_pipeline(imgs, "camA", W, H, R, C, SQ)
    assert not out.success
    assert out.failure_reason == "DEGENERATE_POSE_COVERAGE"
    assert out.coverage is not None
    assert out.coverage.svd_ratio < 0.05


def test_corrupted_and_missing_board_images_are_reported_per_image(data_dir):
    from app.calibration import calibrate_pipeline

    imgs = read_images("corrupted")
    imgs.append(("also_missing.png", b"not an image at all"))
    out = calibrate_pipeline(imgs, "camA", W, H, R, C, SQ)
    assert not out.success
    by_name = {t.filename: t for t in out.traces}
    assert by_name["not_an_image.png"].reason == "decode_failed"
    assert by_name["no_board.png"].reason == "corner_detection_failed"
    assert by_name["wrong_resolution.png"].reason == "resolution_mismatch"
    assert by_name["wrong_resolution.png"].width == W
    assert by_name["wrong_resolution.png"].height == H - 120
    assert by_name["also_missing.png"].reason == "decode_failed"


def test_wrong_board_size_fails_corner_detection(data_dir):
    from app.calibration import calibrate_pipeline

    imgs = read_images("valid_views")
    out = calibrate_pipeline(imgs, "camA", W, H, board_rows=7, board_cols=10, square_mm=SQ)
    assert not out.success
    assert out.failure_reason == "CORNER_DETECTION_FAILED"
    assert all(t.reason == "corner_detection_failed" for t in out.traces)


def test_too_few_views_insufficient(data_dir):
    from app.calibration import calibrate_pipeline

    imgs = read_images("valid_views")[:10]
    out = calibrate_pipeline(imgs, "camA", W, H, R, C, SQ)
    assert not out.success
    assert out.failure_reason == "INSUFFICIENT_VIEWS"


def test_every_trace_is_auditable(data_dir):
    from app.calibration import calibrate_pipeline

    imgs = read_images("corrupted")[:2] + read_images("valid_views")[:14]
    out = calibrate_pipeline(imgs, "camA", W, H, R, C, SQ)
    assert len(out.traces) == 16
    for t in out.traces:
        assert t.sha256 and len(t.sha256) == 64
        assert t.status in {"accepted", "rejected"}
        if t.status == "rejected":
            assert t.reason != "accepted" and t.detail


def test_coherent_but_warped_view_rejected_as_outlier(data_dir):
    """A view whose corners are all detected but are inconsistent with one
    pinhole model must be excluded on reprojection error (and the measured
    error is retained on the trace for audit)."""
    import cv2
    import sys

    sys.path.insert(0, "scripts")
    import generate_fixtures as gf
    from app.calibration import calibrate_pipeline

    K, D = gf.ground_truth_camera(W, H)
    rvec, tvec = gf.make_pose(12, -9, 1000.0, 0.0, (140.0, 0.0))
    img = gf.render_view(rvec, tvec, K, D, W, H)
    xs, ys = np.meshgrid(np.arange(W), np.arange(H))
    dx = 22.0 * np.sin((ys - H / 2) / 60.0) * 0.5 * np.tanh((xs - W / 2) / 120.0)
    dy = 11.0 * np.cos((xs - 900) / 60.0) * 0.5 * np.tanh((ys - H / 2) / 120.0)
    warped = cv2.remap(
        img,
        (xs - dx).astype(np.float32),
        (ys - dy).astype(np.float32),
        cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT,
    )
    png = cv2.imencode(".png", warped)[1].tobytes()

    imgs = read_images("valid_views") + [("warped.png", png)]
    out = calibrate_pipeline(imgs, "camA", W, H, R, C, SQ)
    assert out.success, out.failure_detail
    assert len(out.accepted_indices) == 24
    bad = [t for t in out.traces if t.filename == "warped.png"][0]
    assert bad.status == "rejected"
    assert bad.reason == "outlier_reprojection_error"
    assert bad.reprojection_error_px is not None
    assert bad.reprojection_error_px > 1.0
    assert "threshold" in bad.detail
