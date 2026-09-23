"""FastAPI application: numerical inverse kinematics service."""

from __future__ import annotations

import numpy as np
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from . import robot
from .auth import require_signature
from .schemas import HealthResponse, IKRequest

app = FastAPI(
    title="6-DOF Serial Manipulator IK Service",
    version="1.0.0",
    description=(
        "Numerical inverse kinematics with damped least-squares iteration "
        "from multiple initial guesses. Every success is independently "
        "verified by forward kinematics; failures are separated into "
        "unreachable / singular_no_convergence / joint_limit_conflict."
    ),
)


@app.get("/health", response_model=HealthResponse, tags=["meta"])
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        model="6R PUMA-class arm (Craig modified DH, metres/radians)",
        reach_max_m=round(robot.REACH_MAX, 4),
    )


@app.post("/api/v1/ik/solve",
          dependencies=[Depends(require_signature)],
          tags=["ik"])
async def solve(req: IKRequest):
    R = req.rotation_array()
    p = req.position_array()
    current = (np.asarray(req.current_joints, dtype=float)
               if req.current_joints is not None else None)
    w = (np.asarray(req.weights, dtype=float)
         if req.weights is not None else np.ones(6))

    result = robot.solve_ik(R, p, current_q=current, weights=w, seed=req.seed)
    payload = result.as_dict(current, w)
    # Hard safety gate: an "ok" response must carry a passed FK verification.
    if result.status == "ok":
        v = payload.get("verification") or {}
        if not v.get("passed"):
            return JSONResponse(status_code=500, content={
                "status": "internal_error",
                "reason": "solver produced an unverified candidate; "
                          "withholding success result",
            })
    # Kinematic failures are reported with 200 + status field so clients
    # branch on machine-readable status rather than HTTP errors.
    payload["status_code"] = 200
    return payload
