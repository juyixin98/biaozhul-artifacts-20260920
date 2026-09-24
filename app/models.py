"""Pydantic wire models for the segmentation HTTP API."""

from __future__ import annotations

import math
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field


def _require_finite(v: float) -> float:
    if not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError("coordinates must be finite numbers")
    return float(v)


FiniteFloat = Annotated[float, AfterValidator(_require_finite)]


class Point(BaseModel):
    id: int = Field(..., ge=0, description="Caller-assigned point ID; echoed back.")
    x: FiniteFloat
    y: FiniteFloat
    z: FiniteFloat


class RansacOptions(BaseModel):
    distance_threshold: float = Field(0.10, gt=0.0, le=10.0)
    max_iterations: int = Field(1000, ge=1, le=100_000)
    min_iterations: int = Field(20, ge=1, le=10_000)
    confidence: float = Field(0.99, gt=0.0, lt=1.0)
    min_points: int = Field(12, ge=3, le=10_000)
    min_inlier_count: int = Field(8, ge=3, le=100_000)
    min_inlier_ratio: float = Field(0.50, gt=0.0, le=1.0)
    max_tilt_deg: float = Field(20.0, gt=0.0, le=89.9)
    max_rms: float = Field(0.05, gt=0.0, le=10.0)
    seed_band_quantile: float = Field(0.25, gt=0.0, le=1.0)
    seed_sample_probability: float = Field(0.7, ge=0.0, le=1.0)
    rng_seed: int = Field(20260923, ge=0)
    refit_rounds: int = Field(2, ge=0, le=10)


class ChunkOptions(BaseModel):
    chunk_size: float = Field(4.0, gt=0.0, le=1000.0)
    overlap: float = Field(0.25, ge=0.0, le=0.9)
    min_points: int = Field(12, ge=1, le=100_000)
    normal_k: int = Field(8, ge=3, le=100)
    cos_normal_strong: float = Field(0.90, gt=0.0, le=1.0)
    cos_normal_weak: float = Field(0.75, gt=0.0, le=1.0)
    vertical_normal_z: float = Field(0.35, ge=0.0, le=1.0)
    inner_distance_fraction: float = Field(0.5, gt=0.0, le=1.0)
    non_ground_margin_fraction: float = Field(2.0, gt=1.0, le=20.0)
    enable_local_normals: bool = True


class SegmentRequest(BaseModel):
    points: list[Point] = Field(..., min_length=1)
    ransac: RansacOptions | None = None
    chunk: ChunkOptions | None = None

    def point_array(self) -> list[list[float]]:
        return [[p.x, p.y, p.z] for p in self.points]
