"""API 数据模型（Pydantic v2）。字段命名对外稳定，是服务协议的一部分。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RejectedImage(BaseModel):
    image_id: str
    filename: str
    stage: Literal["decode", "resolution", "corner", "duplicate", "outlier"]
    reason: str
    rms_px: float | None = None
    detail: dict = Field(default_factory=dict)


class KeptImage(BaseModel):
    image_id: str
    filename: str
    rms_px: float
    pose: dict


class VersionSummary(BaseModel):
    version_id: str
    camera_id: str
    resolution: tuple[int, int]
    board: dict
    created_at: str
    image_count: int
    accepted_count: int
    rejected_count: int
    overall_rms_px: float
    state: Literal["active"] = "active"


class CalibrationResult(BaseModel):
    version_id: str
    camera_id: str
    resolution: tuple[int, int]
    board: dict
    created_at: str
    image_count: int
    accepted_count: int
    rejected_count: int
    overall_rms_px: float
    intrinsics: dict
    distortion: dict
    coverage: dict
    accepted_images: list[KeptImage]
    rejected_images: list[RejectedImage]
    rejection_history: list[dict] = Field(default_factory=list)
    thresholds: dict = Field(default_factory=dict)
    provenance: dict
    signature: str
    state: Literal["active"] = "active"


class FailureDetail(BaseModel):
    code: str
    reason: str
    resolution: tuple[int, int] | None = None
    detections: list[dict] = Field(default_factory=list)
    rejected_images: list[RejectedImage] = Field(default_factory=list)
    coverage: dict | None = None
    thresholds: dict = Field(default_factory=dict)
    rejected_count: int | None = None
    accepted_count: int | None = None


class VerifyResponse(BaseModel):
    version_id: str
    valid: bool
    reason: str | None = None
    payload_sha256: str | None = None
