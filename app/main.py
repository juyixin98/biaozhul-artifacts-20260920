"""FastAPI service exposing offline 2D laser scan deskewing.

Run with:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from typing import List, Tuple

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .deskew import PoseCoverageError, deskew_scan

app = FastAPI(
    title="laser-deskew",
    description="Offline 2D laser scan motion-distortion correction",
    version="0.1.0",
)


class Pose(BaseModel):
    """Body pose sample T_world_body at one instant."""

    time: float
    x: float
    y: float
    theta: float


class ScanPoint(BaseModel):
    """One polar measurement: (angle, range) sampled at `time`."""

    time: float
    angle: float = Field(description="beam angle in the laser frame, rad")
    range: float = Field(ge=0.0, description="measured range, m")


class Extrinsic(BaseModel):
    """Sensor extrinsic T_body_laser: laser frame expressed in body frame."""

    x: float = 0.0
    y: float = 0.0
    theta: float = 0.0


class DeskewRequest(BaseModel):
    reference_time: float = Field(
        description="all output points are expressed in the laser frame at this time"
    )
    extrinsic: Extrinsic = Extrinsic()
    poses: List[Pose] = Field(min_length=2)
    points: List[ScanPoint] = Field(min_length=1)


class CorrectedPoint(BaseModel):
    x: float
    y: float


class DeskewResponse(BaseModel):
    reference_time: float
    frame: str = "laser@reference_time"
    points: List[CorrectedPoint]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/deskew", response_model=DeskewResponse)
def deskew(req: DeskewRequest) -> DeskewResponse:
    pose_times = np.array([p.time for p in req.poses])
    pose_xytheta = np.array([[p.x, p.y, p.theta] for p in req.poses])
    point_times = np.array([p.time for p in req.points])
    angles = np.array([p.angle for p in req.points])
    ranges = np.array([p.range for p in req.points])

    try:
        result = deskew_scan(
            point_times=point_times,
            angles=angles,
            ranges=ranges,
            pose_times=pose_times,
            pose_xytheta=pose_xytheta,
            reference_time=req.reference_time,
            extrinsic_xytheta=(
                req.extrinsic.x,
                req.extrinsic.y,
                req.extrinsic.theta,
            ),
        )
    except PoseCoverageError as exc:
        # Client asked for times the pose trajectory does not cover:
        # reject explicitly instead of extrapolating.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return DeskewResponse(
        reference_time=result.reference_time,
        points=[CorrectedPoint(x=float(x), y=float(y)) for x, y in result.points_xy],
    )
