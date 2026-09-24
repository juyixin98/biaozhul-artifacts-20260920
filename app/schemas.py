"""Pydantic models for the deskew service API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Pose(BaseModel):
    """One body pose sample: T_W_B at time ``t`` (body pose in world frame)."""

    t: float
    x: float
    y: float
    theta: float = Field(description="yaw of the body frame in the world frame, rad")


class Extrinsic(BaseModel):
    """Constant extrinsic T_B_L: pose of the laser frame in the body frame."""

    x: float = 0.0
    y: float = 0.0
    theta: float = 0.0


class DeskewRequest(BaseModel):
    """One 2D scan plus the body pose sequence needed to deskew it.

    All times share one clock (seconds). ``ranges``/``angles`` are polar
    coordinates in the laser frame, sampled at ``point_times``. The pose
    sequence must cover every point time and ``reference_time``.
    """

    ranges: list[float]
    angles: list[float]
    point_times: list[float]
    poses: list[Pose]
    reference_time: float
    extrinsic: Extrinsic = Extrinsic()


class DeskewResponse(BaseModel):
    reference_time: float
    frame: str = "laser@reference_time"
    points: list[list[float]]  # (M, 2) x, y in the laser frame at reference_time


class ErrorResponse(BaseModel):
    detail: str
