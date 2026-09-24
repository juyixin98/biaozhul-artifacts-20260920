"""Pydantic request/response models for the evaluation API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Vec3 = tuple[float, float, float]
QuatXYZW = tuple[float, float, float, float]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Pose(StrictModel):
    time: float = Field(..., description="Timestamp in seconds.")
    position: Vec3 = Field(..., description="Translation (x, y, z) in metres.")
    quaternion_xyzw: QuatXYZW = Field(
        ..., description="Rotation quaternion (qx, qy, qz, qw), Hamilton convention."
    )

    @field_validator("time")
    @classmethod
    def _time_finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("time must be finite")
        return v

    @field_validator("position", "quaternion_xyzw")
    @classmethod
    def _all_finite(cls, v: tuple[float, ...]) -> tuple[float, ...]:
        if any(x != x or x in (float("inf"), float("-inf")) for x in v):
            raise ValueError("components must be finite")
        return v


class AssociationConfig(StrictModel):
    max_time_diff: float = Field(
        0.02, gt=0, description="Maximum |t_est - t_gt| in seconds for a match."
    )


class AlignmentConfig(StrictModel):
    mode: Literal["rigid", "similarity"] = Field(
        "rigid",
        description=(
            "'rigid' = SE(3) rotation+translation (scale fixed at 1); "
            "'similarity' = Sim(3) with an estimated uniform scale."
        ),
    )


class RPEConfig(StrictModel):
    delta_index: int = Field(
        1, ge=1, description="Nominal index span between the two matched poses of a pair."
    )
    tolerance_index: int = Field(
        0,
        ge=0,
        description="Accept spans delta_index +/- tolerance_index; nearest span wins.",
    )


class EvaluateRequest(StrictModel):
    estimated: list[Pose] = Field(..., min_length=1)
    ground_truth: list[Pose] = Field(..., min_length=1)
    association: AssociationConfig = Field(default_factory=AssociationConfig)
    alignment: AlignmentConfig = Field(default_factory=AlignmentConfig)
    rpe: RPEConfig = Field(default_factory=RPEConfig)
