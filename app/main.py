"""FastAPI 服务: 二维双连杆机械臂正/逆运动学。

启动: uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import math
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.kinematics import (
    ArmModel,
    IKResult,
    JointLimits,
    forward_kinematics,
    inverse_kinematics,
    solve_path,
)

app = FastAPI(title="2-Link Planar Arm IK", version="1.0.0")


class LimitsIn(BaseModel):
    theta1_min: float = -math.pi
    theta1_max: float = math.pi
    theta2_min: float = -math.pi
    theta2_max: float = math.pi

    @field_validator("theta1_max", "theta2_max")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("limits must be finite")
        return v

    def to_model(self) -> JointLimits:
        if self.theta1_min > self.theta1_max or self.theta2_min > self.theta2_max:
            raise ValueError("limit lower bound exceeds upper bound")
        return JointLimits(
            theta1_min=self.theta1_min,
            theta1_max=self.theta1_max,
            theta2_min=self.theta2_min,
            theta2_max=self.theta2_max,
        )


class ArmIn(BaseModel):
    l1: float = Field(default=1.0, gt=0)
    l2: float = Field(default=1.0, gt=0)
    limits: LimitsIn = Field(default_factory=LimitsIn)

    def to_model(self) -> ArmModel:
        return ArmModel(l1=self.l1, l2=self.l2, limits=self.limits.to_model())


class FKRequest(BaseModel):
    theta1: float
    theta2: float
    arm: ArmIn = Field(default_factory=ArmIn)


class IKRequest(BaseModel):
    x: float
    y: float
    arm: ArmIn = Field(default_factory=ArmIn)


class PathRequest(BaseModel):
    points: list[list[float]]
    seed: Optional[list[float]] = None  # [theta1, theta2], 用于首点选支
    arm: ArmIn = Field(default_factory=ArmIn)

    @field_validator("points")
    @classmethod
    def _check_points(cls, pts: list[list[float]]) -> list[list[float]]:
        if not pts:
            raise ValueError("points must be non-empty")
        for p in pts:
            if len(p) != 2 or not all(math.isfinite(v) for v in p):
                raise ValueError("each point must be [x, y] of finite numbers")
        return pts


def _ik_result_to_dict(res: IKResult) -> dict:
    return {
        "status": res.status.value,
        "reach_distance": res.reach_distance,
        "singular": res.singular,
        "solutions": [
            {
                "branch": s.branch,
                "theta1": s.theta1,
                "theta2": s.theta2,
                "within_limits": s.within_limits,
            }
            for s in res.solutions
        ],
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/fk")
def fk(req: FKRequest) -> dict:
    model = req.arm.to_model()
    x, y = forward_kinematics(model, req.theta1, req.theta2)
    return {"x": x, "y": y}


@app.post("/ik")
def ik(req: IKRequest) -> dict:
    if not (math.isfinite(req.x) and math.isfinite(req.y)):
        raise HTTPException(status_code=422, detail="x and y must be finite")
    model = req.arm.to_model()
    return _ik_result_to_dict(inverse_kinematics(model, req.x, req.y))


@app.post("/ik/path")
def ik_path(req: PathRequest) -> dict:
    model = req.arm.to_model()
    seed = None
    if req.seed is not None:
        if len(req.seed) != 2 or not all(math.isfinite(v) for v in req.seed):
            raise HTTPException(status_code=422, detail="seed must be [theta1, theta2]")
        seed = (req.seed[0], req.seed[1])
    steps = solve_path(model, [tuple(p) for p in req.points], seed=seed)
    return {
        "steps": [
            {
                "target": list(s["target"]),
                "status": s["status"],
                "branch": s["branch"],
                "theta1": s["theta1"],
                "theta2": s["theta2"],
                "step_jump": s["step_jump"],
            }
            for s in steps
        ]
    }
