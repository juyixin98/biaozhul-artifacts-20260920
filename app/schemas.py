"""Pydantic request/response models for the evaluation API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Pose(BaseModel):
    timestamp: float = Field(..., description="Sample time in seconds")
    position: list[float] = Field(..., min_length=3, max_length=3)
    orientation: list[float] = Field(
        ...,
        min_length=4,
        max_length=4,
        description="Quaternion [w, x, y, z] (need not be pre-normalized)",
    )


class RpeSpec(BaseModel):
    delta: float = Field(..., gt=0, description="Time span in seconds")
    tolerance: float = Field(
        0.0, ge=0, description="Accepted deviation from delta in seconds"
    )


class EvalRequest(BaseModel):
    estimated: list[Pose] = Field(..., min_length=1)
    ground_truth: list[Pose] = Field(..., min_length=1)
    max_time_diff: float = Field(
        ..., ge=0, description="Nearest-neighbour association gate (seconds)"
    )
    align_mode: Literal["rigid", "similarity"] = Field(
        "rigid",
        description=(
            "rigid = SE(3), preserves GT metric scale; "
            "similarity = Sim(3), fits a scale factor (reported separately)"
        ),
    )
    rpe: list[RpeSpec] = Field(
        default_factory=list,
        description="Zero or more fixed time spans for RPE evaluation",
    )

    @field_validator("estimated", "ground_truth")
    @classmethod
    def _finite(cls, poses: list[Pose]) -> list[Pose]:
        for i, p in enumerate(poses):
            if not all(map(_is_finite, p.position)):
                raise ValueError(f"pose {i}: non-finite position")
            if not all(map(_is_finite, p.orientation)):
                raise ValueError(f"pose {i}: non-finite orientation")
            if not _is_finite(p.timestamp):
                raise ValueError(f"pose {i}: non-finite timestamp")
        return poses


def _is_finite(x: float) -> bool:
    return x == x and x not in (float("inf"), float("-inf"))
