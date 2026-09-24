"""Offline chessboard camera calibration pipeline.

Real computation throughout: cv2.findChessboardCorners / cornerSubPix /
calibrateCamera / projectPoints. No fixed or canned matrices.

Pipeline (every decision is recorded per image in the returned trace):
  1. decode + resolution check
  2. chessboard corner detection + sub-pixel refinement
  3. first calibration -> duplicate-pose detection
  4. re-calibrate -> iterative reprojection outlier rejection
  5. minimum sample count check
  6. pose-coverage degeneracy check
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import config
from .crypto import sha256_bytes
from .models import (
    IMG_ACCEPTED,
    IMG_CALIBRATION_ERROR,
    IMG_CORNER_FAILED,
    IMG_DECODE_FAILED,
    IMG_DUPLICATE_POSE,
    IMG_OUTLIER,
    IMG_RESOLUTION_MISMATCH,
    CoverageReport,
)

TERM_CRITERIA = (
    cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER,
    config.SUBPIX_MAX_ITER,
    config.SUBPIX_EPS,
)


@dataclass
class ImageTrace:
    index: int
    filename: str
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    # Provisionally accepted: each rejection stage flips this to "rejected".
    status: str = "accepted"
    reason: str = IMG_ACCEPTED
    detail: str | None = None
    duplicate_of: int | None = None
    reprojection_error_px: float | None = None
    rvec: list[float] | None = None
    tvec: list[float] | None = None

    def reject(self, reason: str, detail: str | None = None) -> None:
        self.status = "rejected"
        self.reason = reason
        self.detail = detail


@dataclass
class PipelineOutcome:
    success: bool
    failure_reason: str | None = None
    failure_detail: str | None = None
    traces: list[ImageTrace] = field(default_factory=list)
    rms: float | None = None
    camera_matrix: np.ndarray | None = None
    dist_coeffs: np.ndarray | None = None
    coverage: CoverageReport | None = None
    accepted_indices: list[int] = field(default_factory=list)


def decode_image(data: bytes) -> np.ndarray | None:
    buf = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    return img  # None on failure


def detect_corners(
    img: np.ndarray, board_rows: int, board_cols: int
) -> np.ndarray | None:
    """Return (N,2) float32 refined corners or None."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    ok, corners = cv2.findChessboardCorners(
        gray,
        (board_cols, board_rows),
        flags=cv2.CALIB_CB_ADAPTIVE_THRESH
        | cv2.CALIB_CB_NORMALIZE_IMAGE
        | cv2.CALIB_CB_FAST_CHECK,
    )
    if not ok:
        return None
    refined = cv2.cornerSubPix(
        gray, corners, config.SUBPIX_WIN, (-1, -1), TERM_CRITERIA
    )
    return refined.reshape(-1, 2).astype(np.float64)


def object_points(board_rows: int, board_cols: int, square_mm: float) -> np.ndarray:
    objp = np.zeros((board_rows * board_cols, 3), np.float64)
    xs, ys = np.meshgrid(
        np.arange(board_cols), np.arange(board_rows)
    )
    objp[:, :2] = np.stack([xs.ravel(), ys.ravel()], axis=1) * square_mm
    return objp


def run_calibration(
    objp: np.ndarray,
    image_points: list[np.ndarray],
    image_size: tuple[int, int],
) -> tuple[bool, float, np.ndarray, np.ndarray, list[np.ndarray], list[np.ndarray]]:
    """cv2.calibrateCamera wrapper. Returns (ok, rms, K, D, rvecs, tvecs).

    Standard 5-coefficient vector (k1, k2, p1, p2, k3) with k3 fixed to
    zero: k3 is only identifiable from corners in the extreme periphery,
    and leaving it free otherwise drags k1/k2 into nonsense while not
    changing the reprojection error at all.
    """
    flags = cv2.CALIB_FIX_K3
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 200, 1e-8)
    try:
        rms, K, D, rvecs, tvecs = cv2.calibrateCamera(
            [objp.astype(np.float32)] * len(image_points),
            [p.astype(np.float32) for p in image_points],
            image_size,
            None,
            None,
            flags=flags,
            criteria=criteria,
        )
    except cv2.error:
        return False, math.nan, np.empty(0), np.empty(0), [], []
    if not np.isfinite(K).all() or not np.isfinite(D).all():
        return False, math.nan, K, D, rvecs, tvecs
    return True, float(rms), K, D, rvecs, tvecs


def per_view_errors(
    objp: np.ndarray,
    image_points: list[np.ndarray],
    K: np.ndarray,
    D: np.ndarray,
    rvecs: list[np.ndarray],
    tvecs: list[np.ndarray],
) -> list[float]:
    errors: list[float] = []
    for corners, rvec, tvec in zip(image_points, rvecs, tvecs):
        projected, _ = cv2.projectPoints(objp, rvec, tvec, K, D)
        projected = projected.reshape(-1, 2)
        rmse = float(
            np.sqrt(np.mean(np.sum((projected - corners) ** 2, axis=1)))
        )
        errors.append(rmse)
    return errors


def _rotation_vectors(R: np.ndarray) -> np.ndarray:
    rvec, _ = cv2.Rodrigues(R)
    return rvec.reshape(3)


def camera_center(rvec: np.ndarray, tvec: np.ndarray) -> np.ndarray:
    R, _ = cv2.Rodrigues(rvec)
    return (-R.T @ tvec.reshape(3, 1)).reshape(3)


def relative_rotation_angle_deg(r1: np.ndarray, r2: np.ndarray) -> float:
    R1, _ = cv2.Rodrigues(r1)
    R2, _ = cv2.Rodrigues(r2)
    rr, _ = cv2.Rodrigues(R1.T @ R2)
    return float(np.degrees(np.linalg.norm(rr)))


def find_duplicates(
    traces: list[ImageTrace],
    rvecs: dict[int, np.ndarray],
    tvecs: dict[int, np.ndarray],
) -> set[int]:
    """Mark near-identical viewpoints.

    A later view is a duplicate of an earlier accepted view when the relative
    rotation is below DUPLICATE_ANGLE_DEG and the camera-centre distance is
    below DUPLICATE_TRANSLATION_RATIO times the median pairwise centre distance.
    """
    idxs = [t.index for t in traces if t.status == "accepted"]
    if len(idxs) < 2:
        return set()
    centers = {i: camera_center(rvecs[i], tvecs[i]) for i in idxs}
    dists = [
        float(np.linalg.norm(centers[i] - centers[j]))
        for a, i in enumerate(idxs)
        for j in idxs[a + 1 :]
    ]
    median_dist = float(np.median(dists)) if dists else 0.0
    trans_limit = config.DUPLICATE_TRANSLATION_RATIO * median_dist

    dup: set[int] = set()
    for a, i in enumerate(idxs):
        if i in dup:
            continue
        for j in idxs[a + 1 :]:
            if j in dup:
                continue
            angle = relative_rotation_angle_deg(rvecs[i], rvecs[j])
            dist = float(np.linalg.norm(centers[i] - centers[j]))
            if angle < config.DUPLICATE_ANGLE_DEG and dist <= trans_limit:
                dup.add(j)
                traces[j].reject(
                    IMG_DUPLICATE_POSE,
                    detail=(
                        f"relative rotation {angle:.3f} deg, centre distance "
                        f"{dist:.4f} <= {trans_limit:.4f} "
                        f"(ratio {config.DUPLICATE_TRANSLATION_RATIO} of median "
                        f"pairwise distance {median_dist:.4f})"
                    ),
                )
                traces[j].duplicate_of = i
    return dup


def evaluate_coverage(
    rvecs: list[np.ndarray], tvecs: list[np.ndarray]
) -> CoverageReport:
    centers = np.array([camera_center(r, t) for r, t in zip(rvecs, tvecs)])
    tilts: list[float] = []
    rays: list[np.ndarray] = []
    for rvec, tvec in zip(rvecs, tvecs):
        R, _ = cv2.Rodrigues(rvec)
        # angle between board normal in camera frame (R @ z) and optical axis z
        slant = math.degrees(math.acos(float(np.clip(R[2, 2], -1.0, 1.0))))
        tilts.append(slant)
        # view ray: camera centre expressed in board frame, normalized
        c = (-R.T @ tvec.reshape(3, 1)).reshape(3)
        rays.append(c / max(float(np.linalg.norm(c)), 1e-12))

    # SVD ratio of camera-centre spread (supporting metric)
    if len(centers) >= 2:
        singular = np.linalg.svd(centers - centers.mean(axis=0), compute_uv=False)
        svd_ratio = float(singular[-1] / max(singular[0], 1e-12))
    else:
        svd_ratio = 0.0

    tilt_spread = float(max(tilts) - min(tilts)) if tilts else 0.0

    rays_arr = np.array(rays)
    mean_dir = rays_arr.mean(axis=0)
    mean_dir /= max(float(np.linalg.norm(mean_dir)), 1e-12)
    cos_angles = np.clip(rays_arr @ mean_dir, -1.0, 1.0)
    ray_spread = float(np.degrees(np.arccos(float(np.min(cos_angles)))))

    return CoverageReport(
        svd_ratio=svd_ratio,
        tilt_spread_deg=tilt_spread,
        ray_spread_deg=ray_spread,
        camera_centres=np.round(centers, 6).tolist(),
        board_normal_tilts_deg=[round(t, 4) for t in tilts],
    )


def calibrate_pipeline(
    images: list[tuple[str, bytes]],
    camera_id: str,  # noqa: ARG001 - kept for symmetry/logging
    width: int,
    height: int,
    board_rows: int,
    board_cols: int,
    square_mm: float,
) -> PipelineOutcome:
    traces = [ImageTrace(index=i, filename=name) for i, (name, _) in enumerate(images)]
    objp = object_points(board_rows, board_cols, square_mm)
    corners_by_idx: dict[int, np.ndarray] = {}

    # ---- stages 1-2: decode, resolution, corners -----------------------
    for (name, data), tr in zip(images, traces):
        tr.sha256 = sha256_bytes(data)
        img = decode_image(data)
        if img is None:
            tr.reject(IMG_DECODE_FAILED, "image could not be decoded")
            continue
        h, w = img.shape[:2]
        tr.width, tr.height = w, h
        if (w, h) != (width, height):
            tr.reject(
                IMG_RESOLUTION_MISMATCH,
                detail=f"image is {w}x{h}, job is bound to {width}x{height}",
            )
            continue
        corners = detect_corners(img, board_rows, board_cols)
        if corners is None:
            tr.reject(
                IMG_CORNER_FAILED,
                detail=f"no {board_cols}x{board_rows} inner-corner chessboard found",
            )
            continue
        corners_by_idx[tr.index] = corners

    corner_ok = [t for t in traces if t.index in corners_by_idx]

    if len(corner_ok) < config.MIN_VIEWS:
        reason = (
            "CORNER_DETECTION_FAILED"
            if any(t.reason == IMG_CORNER_FAILED for t in traces)
            else "INSUFFICIENT_VIEWS"
        )
        corner_fail_n = sum(t.reason == IMG_CORNER_FAILED for t in traces)
        detail = (
            f"{len(corner_ok)} view(s) with corners "
            f"(minimum {config.MIN_VIEWS}); {corner_fail_n} corner failure(s)"
        )
        return PipelineOutcome(False, reason, detail, traces)

    # ---- stage 3: initial calibration + duplicate-pose detection -------
    active = [t.index for t in corner_ok]
    ok, rms, K, D, rvecs, tvecs = run_calibration(
        objp, [corners_by_idx[i] for i in active], (width, height)
    )
    if not ok:
        return PipelineOutcome(
            False,
            "CORNER_DETECTION_FAILED",
            "initial cv2.calibrateCamera failed (check board geometry / corner order)",
            traces,
        )
    rvec_map = {i: rvecs[a].copy() for a, i in enumerate(active)}
    tvec_map = {i: tvecs[a].copy() for a, i in enumerate(active)}
    find_duplicates(traces, rvec_map, tvec_map)

    active = [t.index for t in traces if t.status == "accepted"]
    if len(active) < config.MIN_VIEWS:
        return PipelineOutcome(
            False,
            "INSUFFICIENT_VIEWS",
            f"{len(active)} non-duplicate view(s) after duplicate-pose removal "
            f"(minimum {config.MIN_VIEWS})",
            traces,
        )

    # ---- stage 4: iterative reprojection outlier rejection -------------
    rms = math.nan
    K = D = np.empty(0)
    rvec_list = tvec_list = []
    for _iteration in range(config.OUTLIER_MAX_ITERATIONS + 1):
        ok, rms, K, D, rvec_list, tvec_list = run_calibration(
            objp, [corners_by_idx[i] for i in active], (width, height)
        )
        if not ok:
            for i in active:
                traces[i].reject(IMG_CALIBRATION_ERROR, "cv2.calibrateCamera failed")
            return PipelineOutcome(
                False, "INSUFFICIENT_VIEWS", "calibration failed during outlier loop", traces
            )
        errors = per_view_errors(
            objp,
            [corners_by_idx[i] for i in active],
            K,
            D,
            rvec_list,
            tvec_list,
        )
        median_err = float(np.median(errors))
        threshold = max(config.OUTLIER_MAX_PX, config.OUTLIER_MEDIAN_K * median_err)
        offenders = [
            (i, e) for i, e in zip(active, errors) if e > threshold
        ]
        # record current errors/poses on the trace for full auditability
        for i, e, rv, tv in zip(active, errors, rvec_list, tvec_list):
            traces[i].reprojection_error_px = round(e, 6)
            traces[i].rvec = np.round(rv.reshape(3), 8).tolist()
            traces[i].tvec = np.round(tv.reshape(3), 8).tolist()
        if not offenders:
            break
        if len(active) - len(offenders) < config.MIN_VIEWS:
            # reject only what we can while staying above the floor; the job
            # must still fail as INSUFFICIENT_VIEWS (not silently ship bad data)
            offenders.sort(key=lambda x: -x[1])
            keepable = len(active) - config.MIN_VIEWS
            for i, e in offenders[:keepable]:
                traces[i].reject(
                    IMG_OUTLIER,
                    detail=f"reprojection RMSE {e:.4f}px > threshold {threshold:.4f}px",
                )
            for i, e in offenders[keepable:]:
                traces[i].reprojection_error_px = round(e, 6)
            active = [t.index for t in traces if t.status == "accepted"]
            return PipelineOutcome(
                False,
                "INSUFFICIENT_VIEWS",
                f"{len(offenders)} view(s) above {threshold:.3f}px reprojection "
                f"threshold; only {len(active)} clean view(s) remain "
                f"(minimum {config.MIN_VIEWS})",
                traces,
            )
        for i, e in offenders:
            tr = traces[i]
            tr.reject(
                IMG_OUTLIER,
                detail=f"reprojection RMSE {e:.4f}px > threshold {threshold:.4f}px",
            )
            # keep the measured error/pose for auditability of the rejection
            tr.reprojection_error_px = round(e, 6)
        active = [t.index for t in traces if t.status == "accepted"]

    # Clear per-view fields, then repopulate from the FINAL calibration.
    # Outliers keep the error recorded at the iteration that removed them.
    for tr in traces:
        tr.rvec = tr.tvec = None
        if tr.reason != IMG_OUTLIER:
            tr.reprojection_error_px = None
    for i, e, rv, tv in zip(active, errors, rvec_list, tvec_list):
        traces[i].status = IMG_ACCEPTED
        traces[i].reason = IMG_ACCEPTED
        traces[i].reprojection_error_px = round(e, 6)
        traces[i].rvec = np.round(rv.reshape(3), 8).tolist()
        traces[i].tvec = np.round(tv.reshape(3), 8).tolist()

    # ---- stage 6: pose-coverage degeneracy -----------------------------
    # Two geometrically distinct failure modes are rejected independently:
    #   A. camera centres nearly collinear/coplanar (svd_ratio ~ 0): pure
    #      translation on a line/plane, no real baseline shape;
    #   B. views share one orientation (low tilt AND low view-ray spread):
    #      fronto-parallel boards seen from a tight direction cone, which
    #      leaves focal length / principal point unconstrained.
    coverage = evaluate_coverage(rvec_list, tvec_list)
    if coverage.svd_ratio < config.COVERAGE_SVD_RATIO:
        return PipelineOutcome(
            False,
            "DEGENERATE_POSE_COVERAGE",
            f"camera-centre SVD ratio {coverage.svd_ratio:.4f} < "
            f"{config.COVERAGE_SVD_RATIO}: viewpoints collapse onto a "
            f"line/plane (no baseline shape)",
            traces,
            rms,
            K,
            D,
            coverage,
            active,
        )
    if (
        coverage.tilt_spread_deg < config.COVERAGE_TILT_SPREAD_DEG
        and coverage.ray_spread_deg < config.COVERAGE_RAY_SPREAD_DEG
    ):
        return PipelineOutcome(
            False,
            "DEGENERATE_POSE_COVERAGE",
            f"board tilt spread {coverage.tilt_spread_deg:.2f} deg < "
            f"{config.COVERAGE_TILT_SPREAD_DEG} and view-ray spread "
            f"{coverage.ray_spread_deg:.2f} deg < {config.COVERAGE_RAY_SPREAD_DEG}",
            traces,
            rms,
            K,
            D,
            coverage,
            active,
        )

    return PipelineOutcome(
        True,
        None,
        None,
        traces,
        rms,
        K,
        D,
        coverage,
        active,
    )
