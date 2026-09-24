"""FastAPI service wrapping the occupancy-grid backend.

In-memory grid registry, JSON in/out, synthetic data only — no hardware.
"""

from __future__ import annotations

import threading
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .grid import CellState, GridConfig, OccupancyGrid
from .raycast import RayObservation


class CreateGridRequest(BaseModel):
    nx: int = Field(gt=0)
    ny: int = Field(gt=0)
    resolution: float = Field(default=1.0, gt=0)
    origin_x: float = 0.0
    origin_y: float = 0.0
    p_occ: float = Field(default=0.7, gt=0.0, lt=1.0)
    p_free: float = Field(default=0.3, gt=0.0, lt=1.0)
    p_clamp_max: float = Field(default=0.99, gt=0.0, lt=1.0)
    p_clamp_min: float = Field(default=0.01, gt=0.0, lt=1.0)
    p_prior: float = Field(default=0.5, gt=0.0, lt=1.0)
    occ_threshold: float = 0.0
    free_threshold: float = 0.0


class RayRequest(BaseModel):
    ox: float
    oy: float
    ex: float | None = None
    ey: float | None = None
    angle: float | None = None
    max_range: float | None = None


def create_app() -> FastAPI:
    app = FastAPI(
        title="Occupancy Grid Service",
        version="1.0.0",
        description="2D log-odds occupancy grid updates (synthetic/offline data only).",
    )
    grids: dict[str, OccupancyGrid] = {}
    lock = threading.Lock()

    def _get(grid_id: str) -> OccupancyGrid:
        grid = grids.get(grid_id)
        if grid is None:
            raise HTTPException(status_code=404, detail=f"unknown grid_id: {grid_id}")
        return grid

    @app.post("/grids", status_code=201)
    def create_grid(req: CreateGridRequest):
        try:
            config = GridConfig(**req.model_dump())
            grid = OccupancyGrid(config)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        grid_id = uuid.uuid4().hex
        with lock:
            grids[grid_id] = grid
        return {"grid_id": grid_id, "config": grid.to_dict()["config"]}

    @app.get("/grids")
    def list_grids():
        with lock:
            return {
                "grids": [
                    {"grid_id": gid, "nx": g.config.nx, "ny": g.config.ny}
                    for gid, g in grids.items()
                ]
            }

    @app.get("/grids/{grid_id}")
    def grid_summary(grid_id: str):
        grid = _get(grid_id)
        states = grid.state_map()
        counts = {CellState.UNKNOWN.value: 0, CellState.FREE.value: 0,
                  CellState.OCCUPIED.value: 0}
        for row in states:
            for s in row:
                counts[s] += 1
        return {"grid_id": grid_id, "config": grid.to_dict()["config"], "counts": counts}

    @app.post("/grids/{grid_id}/rays")
    def update_ray(grid_id: str, req: RayRequest):
        grid = _get(grid_id)

        has_endpoint = req.ex is not None and req.ey is not None
        has_direction = req.angle is not None and req.max_range is not None
        partial = (req.ex is not None) != (req.ey is not None)
        if partial:
            raise HTTPException(status_code=422, detail="ex and ey must be given together")
        if not has_endpoint and not has_direction:
            raise HTTPException(
                status_code=422,
                detail="provide (ex, ey) for an echo or (angle, max_range) for no return",
            )

        if has_endpoint:
            ray = RayObservation(ox=req.ox, oy=req.oy, ex=req.ex, ey=req.ey)
            try:
                result = grid.update_ray(ray)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return {"kind": "echo", **result}

        # No return: free segment up to max_range.
        try:
            result = grid.update_no_return(req.ox, req.oy, req.angle, req.max_range)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"kind": "no_return", **result}

    @app.get("/grids/{grid_id}/cells/{ix}/{iy}")
    def get_cell(grid_id: str, ix: int, iy: int):
        grid = _get(grid_id)
        if not grid.in_map(ix, iy):
            raise HTTPException(status_code=400, detail="cell outside the map")
        return {
            "ix": ix,
            "iy": iy,
            "log_odds": grid.log_odds_at(ix, iy),
            "probability": grid.probability_at(ix, iy),
            "state": grid.state_at(ix, iy).value,
        }

    @app.get("/grids/{grid_id}/states")
    def get_states(grid_id: str):
        grid = _get(grid_id)
        return {"states": grid.state_map()}

    @app.get("/grids/{grid_id}/export")
    def export_grid(grid_id: str):
        grid = _get(grid_id)
        return grid.to_dict()

    @app.post("/grids/import")
    def import_grid(data: dict):
        try:
            grid = OccupancyGrid.from_dict(data)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        grid_id = uuid.uuid4().hex
        with lock:
            grids[grid_id] = grid
        return {"grid_id": grid_id, "config": grid.to_dict()["config"]}

    @app.delete("/grids/{grid_id}", status_code=204)
    def delete_grid(grid_id: str):
        with lock:
            if grids.pop(grid_id, None) is None:
                raise HTTPException(status_code=404, detail=f"unknown grid_id: {grid_id}")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()
