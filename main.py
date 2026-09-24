"""FastAPI 应用：小规模离线单目束调整 HTTP 接口。"""

from __future__ import annotations

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Literal

from ba.lie import log_so3
from ba.problem import BAProblem, CameraPose, Intrinsics, Observation
from ba.simulate import scene_to_problem, simulate_scene
from ba.solver import BASolver, SolverOptions, SolveResult

app = FastAPI(
    title="Small Monocular Bundle Adjustment",
    version="1.0.0",
    description="纯后端小规模单目束调整：Schur 补消元 LM 求解，固定内参，锚定首相机与尺度。",
)


# ---------------------------------------------------------------------- #
# 请求/响应模型
# ---------------------------------------------------------------------- #
class IntrinsicsIn(BaseModel):
    fx: float
    fy: float
    cx: float
    cy: float


class CameraIn(BaseModel):
    rvec: list[float] = Field(min_length=3, max_length=3, description="Rodrigues 旋转向量")
    tvec: list[float] = Field(min_length=3, max_length=3)


class ObservationIn(BaseModel):
    camera: int = Field(ge=0, description="相机下标")
    point: int = Field(ge=0, description="三维点下标")
    uv: list[float] = Field(min_length=2, max_length=2)


class ProblemIn(BaseModel):
    intrinsics: IntrinsicsIn
    cameras: list[CameraIn]
    points: list[list[float]] = Field(description="(N,3) 三维点初值")
    observations: list[ObservationIn]


class SolverOptionsIn(BaseModel):
    max_iterations: int = 50
    solver: Literal["schur", "full"] = "schur"
    lambda_init: float = 1e-3
    fix_first_camera: bool = True
    scale_anchor: bool = True
    scale_weight: float = 1e4
    scale_target: float | None = None
    depth_epsilon: float = 1e-6
    cost_tol: float = 1e-12


class SimulateIn(BaseModel):
    n_cameras: int = 5
    n_points: int = 40
    noise_px: float = 1.0
    seed: int = 0
    image_width: int = 640
    image_height: int = 480
    perturb_rot: float = 0.02
    perturb_trans: float = 0.1
    perturb_point: float = 0.3
    n_behind_camera: int = 2
    n_under_observed: int = 2


class SolveSimulatedIn(BaseModel):
    scene: SimulateIn = Field(default_factory=SimulateIn)
    options: SolverOptionsIn = Field(default_factory=SolverOptionsIn)


# ---------------------------------------------------------------------- #
# 序列化
# ---------------------------------------------------------------------- #
def _cam_out(cam: CameraPose) -> dict:
    return {"rvec": [float(x) for x in log_so3(cam.R)], "tvec": [float(x) for x in cam.t]}


def _problem_out(problem: BAProblem) -> dict:
    return {
        "intrinsics": vars(problem.intrinsics),
        "cameras": [_cam_out(c) for c in problem.cameras],
        "points": [[float(x) for x in p] for p in problem.points],
        "observations": [
            {"camera": o.camera, "point": o.point, "uv": [float(o.uv[0]), float(o.uv[1])]}
            for o in problem.observations
        ],
    }


def _to_problem(p: ProblemIn) -> BAProblem:
    points = np.asarray(p.points, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3:
        raise HTTPException(400, "points 必须是 (N,3) 数组")
    try:
        return BAProblem(
            intrinsics=Intrinsics(**vars(p.intrinsics)),
            cameras=[CameraPose.from_rvec_tvec(c.rvec, c.tvec) for c in p.cameras],
            points=points,
            observations=[
                Observation(camera=o.camera, point=o.point, uv=np.asarray(o.uv, dtype=float))
                for o in p.observations
            ],
        )
    except Exception as e:
        raise HTTPException(400, f"问题数据无效: {e}")


def _options_from(o: SolverOptionsIn) -> SolverOptions:
    return SolverOptions(**vars(o))


def _result_out(result: SolveResult) -> dict:
    return {
        "converged": result.converged,
        "iterations": result.iterations,
        "cost_history": result.cost_history,
        "final_cost": result.cost_history[-1],
        "diagnostics": result.diagnostics,
        "solution": _problem_out(result.problem),
    }


def _scene_out(scene: dict, include_gt: bool = True) -> dict:
    out = {
        "problem": _problem_out(scene_to_problem(scene)),
    }
    if include_gt:
        out["ground_truth"] = {
            "cameras": [_cam_out(c) for c in scene["gt_cameras"]],
            "points": [[float(x) for x in p] for p in scene["gt_points"]],
        }
    return out


# ---------------------------------------------------------------------- #
# 接口
# ---------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/v1/simulate", summary="生成带噪声的合成束调整问题（真值已知）")
def simulate(req: SimulateIn):
    scene = simulate_scene(
        n_cameras=req.n_cameras,
        n_points=req.n_points,
        noise_px=req.noise_px,
        seed=req.seed,
        image_size=(req.image_width, req.image_height),
        perturb_rot=req.perturb_rot,
        perturb_trans=req.perturb_trans,
        perturb_point=req.perturb_point,
        n_behind_camera=req.n_behind_camera,
        n_under_observed=req.n_under_observed,
    )
    return _scene_out(scene)


@app.post("/v1/solve", summary="求解束调整（Schur 补或完整正规方程）")
def solve(req: dict):
    """请求体: {"problem": ProblemIn, "options": SolverOptionsIn?}"""
    if "problem" not in req:
        raise HTTPException(400, "缺少 problem 字段")
    problem = _to_problem(ProblemIn.model_validate(req["problem"]))
    options = (
        _options_from(SolverOptionsIn.model_validate(req["options"]))
        if "options" in req and req["options"] is not None
        else SolverOptions()
    )
    result = BASolver(options).solve(problem)
    return _result_out(result)


@app.post("/v1/solve_simulated", summary="一键：生成合成场景并求解")
def solve_simulated(req: SolveSimulatedIn):
    s = req.scene
    scene = simulate_scene(
        n_cameras=s.n_cameras,
        n_points=s.n_points,
        noise_px=s.noise_px,
        seed=s.seed,
        image_size=(s.image_width, s.image_height),
        perturb_rot=s.perturb_rot,
        perturb_trans=s.perturb_trans,
        perturb_point=s.perturb_point,
        n_behind_camera=s.n_behind_camera,
        n_under_observed=s.n_under_observed,
    )
    problem = scene_to_problem(scene)
    result = BASolver(_options_from(req.options)).solve(problem)
    out = _result_out(result)
    out["ground_truth"] = _scene_out(scene)["ground_truth"]
    return out
