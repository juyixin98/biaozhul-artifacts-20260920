"""Runtime configuration. All quality thresholds are overridable via env vars."""
from __future__ import annotations

import os
from pathlib import Path


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw is not None else default


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default


DATA_DIR = Path(os.environ.get("CALIB_DATA_DIR", "data")).resolve()

# Roots that /calibrations/from-paths is allowed to read. cwd is always allowed.
_extra_roots = os.environ.get("CALIB_IMAGE_ROOTS", "")
IMAGE_ROOTS = [Path(p).resolve() for p in _extra_roots.split(":") if p.strip()]
IMAGE_ROOTS.append(Path.cwd().resolve())

# HMAC key: env var wins; otherwise a random key persisted inside DATA_DIR.
SECRET_KEY_ENV = os.environ.get("CALIB_HMAC_KEY")

# --- Quality thresholds -------------------------------------------------
MIN_VIEWS = _int("CALIB_MIN_VIEWS", 12)

# Reprojection outlier rejection: error > max(OUTLIER_MAX_PX, OUTLIER_MEDIAN_K * median)
OUTLIER_MAX_PX = _float("CALIB_OUTLIER_MAX_PX", 1.0)
OUTLIER_MEDIAN_K = _float("CALIB_OUTLIER_MEDIAN_K", 3.0)
OUTLIER_MAX_ITERATIONS = _int("CALIB_OUTLIER_MAX_ITER", 5)

# Duplicate pose detection.
DUPLICATE_ANGLE_DEG = _float("CALIB_DUP_ANGLE_DEG", 2.0)
DUPLICATE_TRANSLATION_RATIO = _float("CALIB_DUP_TRANS_RATIO", 0.08)

# Pose coverage: camera-centre point cloud SVD ratio + board normal tilt spread.
COVERAGE_SVD_RATIO = _float("CALIB_COVERAGE_SVD_RATIO", 0.05)
COVERAGE_TILT_SPREAD_DEG = _float("CALIB_COVERAGE_TILT_DEG", 5.0)
COVERAGE_RAY_SPREAD_DEG = _float("CALIB_COVERAGE_RAY_DEG", 8.0)

# Chessboard refinement termination criteria.
SUBPIX_WIN = (_int("CALIB_SUBPIX_WIN", 11),) * 2
SUBPIX_MAX_ITER = _int("CALIB_SUBPIX_MAX_ITER", 30)
SUBPIX_EPS = _float("CALIB_SUBPIX_EPS", 1e-3)

MAX_UPLOAD_BYTES = _int("CALIB_MAX_UPLOAD_MB", 30) * 1024 * 1024
MAX_IMAGES_PER_JOB = _int("CALIB_MAX_IMAGES", 100)
