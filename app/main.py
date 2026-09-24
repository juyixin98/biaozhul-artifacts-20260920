"""FastAPI 服务层：正解 / 逆解 / 连续路径逆解。

服务无状态、无硬件连接，只处理数值请求。
失败不会通过裁剪目标坐标伪装成功：响应中的 status 明确区分
ok / near_singular / full_extension / full_fold / unreachable / limit_violation。
"""

from __future__ import annotations

import math
from typing import List, Literal, Optional

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

from .kinematics import (
    ContinuousIKSolver,
    IKStatus,
    LinkParams,
    forward_kinematics,
    inverse_kinematics,
)
from .synthetic import acceptance_suite

app = FastAPI(
    title="二维双连杆逆运动学服务",
    version="1.0.0",
    description=(
        "2R planar arm FK/IK service（仅合成数据，无真实硬件，无可视化）。"
        "两支解析逆解 + 关节限位 + 沿路径连续选解，奇异/不可达/限位冲突显式上报。"
    ),
)


# ---------- 请求 / 响应模型 ----------


class LinkParamsModel(BaseModel):
    l1: float = Field(1.0, gt=0, description="第一连杆长度（>0）")
    l2: float = Field(1.0, gt=0, description="第二连杆长度（>0）")
    theta1_min: float = -math.pi
    theta1_max: float = math.pi
    theta2_min: float = -math.pi
    theta2_max: float = math.pi
    reach_tol_ratio: float = Field(1e-9, gt=0, lt=1e-3)
    singular_eps: float = Field(1e-6, gt=0, lt=1.0)

    def to_core(self) -> LinkParams:
        return LinkParams(
            l1=self.l1,
            l2=self.l2,
            theta1_min=self.theta1_min,
            theta1_max=self.theta1_max,
            theta2_min=self.theta2_min,
            theta2_max=self.theta2_max,
            reach_tol_ratio=self.reach_tol_ratio,
            singular_eps=self.singular_eps,
        )


class FKRequest(BaseModel):
    q1: float = Field(..., description="第一关节角（弧度）")
    q2: float = Field(..., description="第二关节相对角（弧度）")
    params: LinkParamsModel = Field(default_factory=LinkParamsModel)


class Point(BaseModel):
    x: float
    y: float

    @field_validator("x", "y")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("坐标必须是有限实数")
        return v


class IKRequest(BaseModel):
    target: Point
    params: LinkParamsModel = Field(default_factory=LinkParamsModel)
    previous_joints: Optional[List[float]] = Field(
        None,
        min_length=2,
        max_length=2,
        description="上一可行点关节角 [q1,q2]，用于连续选解",
    )
    preference: Optional[Literal["elbow_up", "elbow_down"]] = Field(
        None, description="显式肘部分支偏好；撞限位时直接报 limit_violation"
    )
    hysteresis: float = Field(
        1e-6,
        ge=0,
        description="同名分支滞回系数（两支代价接近时抑制抖动），0 表示纯最短角距离",
    )
    w1: float = Field(1.0, gt=0, description="q1 连续性权重")
    w2: float = Field(1.0, gt=0, description="q2 连续性权重")


class PathRequest(BaseModel):
    targets: List[Point] = Field(..., min_length=1, description="目标点序列")
    params: LinkParamsModel = Field(default_factory=LinkParamsModel)
    hysteresis: float = Field(1e-6, ge=0)
    w1: float = Field(1.0, gt=0)
    w2: float = Field(1.0, gt=0)


def _params(model: LinkParamsModel) -> LinkParams:
    try:
        return model.to_core()
    except ValueError as exc:  # 限位区间非法等
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------- 路由 ----------


@app.get("/")
def root() -> dict:
    return {
        "service": "2-link planar IK",
        "version": app.version,
        "docs": "/docs",
        "endpoints": [
            "GET /health",
            "POST /fk",
            "POST /ik",
            "POST /ik/path",
            "GET /synthetic/acceptance",
        ],
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "numpy": np.__version__}


@app.post("/fk")
def fk(req: FKRequest) -> dict:
    """正运动学：关节角 -> 末端位置 / 肘部位置。"""
    p = _params(req.params)
    ee = forward_kinematics(req.q1, req.q2, p)
    elb = (p.l1 * math.cos(req.q1), p.l1 * math.sin(req.q1))
    return {
        "joints": [req.q1, req.q2],
        "end_effector": [float(ee[0]), float(ee[1])],
        "elbow": [float(elb[0]), float(elb[1])],
        "params": req.params.model_dump(),
    }


@app.post("/ik")
def ik(req: IKRequest) -> dict:
    """单点逆解：两支解析解 + 选支结果 + 明确状态。"""
    p = _params(req.params)
    prev = tuple(req.previous_joints) if req.previous_joints is not None else None
    sol = inverse_kinematics(
        (req.target.x, req.target.y),
        p,
        previous_joints=prev,
        preference=req.preference,
        hysteresis=req.hysteresis,
        weights=(req.w1, req.w2),
    )
    out = sol.as_dict()
    out["workspace"] = {
        "r_max": p.reach,
        "r_min": p.inner_radius,
        "target_radius": float(math.hypot(req.target.x, req.target.y)),
    }
    out["success"] = sol.joints is not None
    return out


@app.post("/ik/path")
def ik_path(req: PathRequest) -> dict:
    """沿目标序列连续选解，返回逐点状态与连续性/精度汇总。"""
    p = _params(req.params)
    solver = ContinuousIKSolver(
        p, hysteresis=req.hysteresis, weights=(req.w1, req.w2)
    )
    result = solver.solve_path([(pt.x, pt.y) for pt in req.targets])
    out = result.as_dict()
    out["success"] = result.failed_count == 0
    out["note"] = (
        "全部点求解成功"
        if out["success"]
        else f"{result.failed_count} 个点失败（unreachable/limit_violation），"
        "失败点不更新连续性基准"
    )
    return out


@app.get("/synthetic/acceptance")
def synthetic_acceptance() -> dict:
    """列出内置合成验收场景（正解生成/离线回放），供核对测试覆盖面。"""
    return {
        "description": "所有目标均由正运动学生成或在工作区内参数化合成，无真实硬件",
        "cases": [
            {
                "name": c.name,
                "description": c.description,
                "expect": c.expect,
                "n_targets": len(c.targets),
                "first_target": list(c.targets[0]),
            }
            for c in acceptance_suite()
        ],
    }
