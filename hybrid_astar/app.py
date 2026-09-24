"""FastAPI application exposing the offline planner as an HTTP service."""

from __future__ import annotations

import math

from fastapi import FastAPI

from .grid_map import GridMap
from .models import PlanRequest, PlanResponse, PathPoint
from .planner import HybridAStarPlanner, PlannerConfig
from .vehicle import Vehicle


def create_app() -> FastAPI:
    app = FastAPI(title="Hybrid A* Offline Planner", version="0.1.0")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/plan", response_model=PlanResponse)
    def plan(req: PlanRequest) -> PlanResponse:
        grid = GridMap(req.grid, resolution=req.resolution, origin=req.origin)
        vehicle = Vehicle(**req.vehicle.model_dump())
        cfg = PlannerConfig(
            heading_bins=req.planner.heading_bins,
            primitive_length=req.planner.primitive_length,
            sample_step=req.planner.sample_step,
            reverse_penalty=req.planner.reverse_penalty,
            gear_switch_penalty=req.planner.gear_switch_penalty,
            curvature_penalty=req.planner.curvature_penalty,
            use_heuristic=req.planner.use_heuristic,
            heuristic_weight=req.planner.heuristic_weight,
            allow_reverse=req.planner.allow_reverse,
            max_expansions=req.planner.max_expansions,
            goal_tol_xy=req.planner.goal_tol_xy,
            goal_tol_theta=math.radians(req.planner.goal_tol_theta_deg),
        )
        planner = HybridAStarPlanner(grid, vehicle, cfg)
        result = planner.plan(
            (req.start.x, req.start.y, req.start.theta),
            (req.goal.x, req.goal.y, req.goal.theta),
        )
        return PlanResponse(
            success=result.success,
            message=result.message,
            cost=result.cost if result.success else None,
            expansions=result.expansions,
            elapsed_ms=result.elapsed_ms,
            path=[PathPoint(**p) for p in result.path()],
        )

    return app


app = create_app()
