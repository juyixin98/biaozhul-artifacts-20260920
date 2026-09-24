"""Pydantic protocol models."""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Status(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class FailureReason(str, Enum):
    CORNER_DETECTION_FAILED = "CORNER_DETECTION_FAILED"
    INSUFFICIENT_VIEWS = "INSUFFICIENT_VIEWS"
    DEGENERATE_POSE_COVERAGE = "DEGENERATE_POSE_COVERAGE"


# Per-image rejection / selection reasons
IMG_ACCEPTED = "accepted"
IMG_DECODE_FAILED = "decode_failed"
IMG_RESOLUTION_MISMATCH = "resolution_mismatch"
IMG_CORNER_FAILED = "corner_detection_failed"
IMG_DUPLICATE_POSE = "duplicate_pose"
IMG_OUTLIER = "outlier_reprojection_error"
IMG_CALIBRATION_ERROR = "calibration_error"


class BoardSpec(BaseModel):
    board_rows: int = Field(ge=2, le=100, description="inner corners along rows")
    board_cols: int = Field(ge=2, le=100)
    square_size_mm: float = Field(gt=0, le=10_000)


class CalibrationRequestJson(BoardSpec):
    camera_id: str = Field(min_length=1, max_length=128)
    width: int = Field(ge=16, le=100_000)
    height: int = Field(ge=16, le=100_000)
    image_paths: list[str] = Field(min_length=1, max_length=100)

    @field_validator("camera_id")
    @classmethod
    def _camera_charset(cls, v: str) -> str:
        if not all(c.isalnum() or c in "._-" for c in v):
            raise ValueError("only alphanumeric characters and . _ - are allowed")
        return v


class PoseModel(BaseModel):
    rvec: list[float]
    tvec: list[float]


class ImageReport(BaseModel):
    index: int
    filename: str
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    status: Literal["accepted", "rejected"]
    reason: str
    detail: str | None = None
    duplicate_of: int | None = None
    reprojection_error_px: float | None = None
    pose: PoseModel | None = None


class Intrinsics(BaseModel):
    camera_matrix: list[list[float]]
    dist_coeffs: list[float]
    # Convenience accessors
    fx: float
    fy: float
    cx: float
    cy: float


class CoverageReport(BaseModel):
    svd_ratio: float
    tilt_spread_deg: float
    ray_spread_deg: float
    camera_centres: list[list[float]]
    board_normal_tilts_deg: list[float]


class CalibrationResult(BaseModel):
    rms: float
    per_view_rms_px: float
    max_view_error_px: float
    intrinsics: Intrinsics
    accepted_count: int
    coverage: CoverageReport


class ExclusionSummary(BaseModel):
    decode_failed: int = 0
    resolution_mismatch: int = 0
    corner_detection_failed: int = 0
    duplicate_pose: int = 0
    outlier_reprojection_error: int = 0
    calibration_error: int = 0


class JobReport(BaseModel):
    job_id: str
    camera_id: str
    width: int
    height: int
    board_rows: int
    board_cols: int
    square_size_mm: float
    status: Status
    failure_reason: FailureReason | None = None
    failure_detail: str | None = None
    created_version: int | None = None
    version_id: str | None = None
    result: CalibrationResult | None = None
    images: list[ImageReport]
    exclusions: ExclusionSummary
    thresholds: dict[str, float | int]


class VersionSummary(BaseModel):
    version_id: str
    camera_id: str
    width: int
    height: int
    version: int
    created_at: str
    rms: float
    accepted_count: int
    job_id: str
    signature: str
    signature_valid: bool = True


class VersionDetail(VersionSummary):
    board_rows: int
    board_cols: int
    square_size_mm: float
    result: CalibrationResult
    images: list[ImageReport]


class ResolutionConflict(BaseModel):
    detail: str
    camera_id: str
    requested_width: int
    requested_height: int
    available_resolutions: list[list[int]]
