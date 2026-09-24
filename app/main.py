"""FastAPI service exposing offline 2D laser scan deskewing.

Run with::

    uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import numpy as np
from fastapi import FastAPI, HTTPException

from .deskew import PoseCoverageError, PoseSequenceError, deskew_scan
from .schemas import DeskewRequest, DeskewResponse, ErrorResponse

app = FastAPI(
    title="laser-deskew",
    description="Offline 2D laser scan motion-distortion correction",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/deskew",
    response_model=DeskewResponse,
    responses={400: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def deskew(req: DeskewRequest) -> DeskewResponse:
    if not (len(req.ranges) == len(req.angles) == len(req.point_times)):
        raise HTTPException(
            status_code=422,
            detail="ranges, angles and point_times must have equal length",
        )
    pose_times = np.array([p.t for p in req.poses], dtype=float)
    poses = np.array([[p.x, p.y, p.theta] for p in req.poses], dtype=float)
    ext = req.extrinsic
    try:
        points = deskew_scan(
            ranges=np.asarray(req.ranges, dtype=float),
            angles=np.asarray(req.angles, dtype=float),
            point_times=np.asarray(req.point_times, dtype=float),
            pose_times=pose_times,
            poses=poses,
            reference_time=req.reference_time,
            extrinsic=(ext.x, ext.y, ext.theta),
        )
    except (PoseCoverageError, PoseSequenceError, ValueError) as exc:
        # Insufficient pose coverage or malformed input: reject explicitly
        # instead of extrapolating silently.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DeskewResponse(
        reference_time=req.reference_time,
        points=[[float(x), float(y)] for x, y in points],
    )
