"""FastAPI service exposing the occupancy grid backend over HTTP.

Grids are kept in an in-memory registry keyed by UUID. All data is
synthetic / replayed by the caller; no hardware is involved.

Run with:  uvicorn occupancy_grid.api:app --reload
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .grid import (
    DEFAULT_L_FREE,
    DEFAULT_L_MAX,
    DEFAULT_L_MIN,
    DEFAULT_L_OCC,
    OccupancyGrid,
)
from .io import load_grid, save_grid

app = FastAPI(title="Occupancy Grid Backend", version="0.1.0")

_grids: Dict[str, OccupancyGrid] = {}


# ----------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------
class GridCreate(BaseModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    resolution: float = Field(gt=0)
    origin_x: float = 0.0
    origin_y: float = 0.0
    l_occ: float = DEFAULT_L_OCC
    l_free: float = DEFAULT_L_FREE
    l_min: float = DEFAULT_L_MIN
    l_max: float = DEFAULT_L_MAX


class GridInfo(BaseModel):
    grid_id: str
    width: int
    height: int
    resolution: float
    origin_x: float
    origin_y: float


class RayIn(BaseModel):
    sensor_x: float
    sensor_y: float
    end_x: float
    end_y: float
    hit: bool


class RaysRequest(BaseModel):
    rays: List[RayIn]


class ScanRequest(BaseModel):
    pose_x: float
    pose_y: float
    pose_theta: float = 0.0
    angles: List[float]
    ranges: List[float]
    max_range: float = Field(gt=0)


class UpdateResult(BaseModel):
    grid_id: str
    cells_updated: int


class PathRequest(BaseModel):
    path: str


class CellQuery(BaseModel):
    ix: int
    iy: int


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _get_grid(grid_id: str) -> OccupancyGrid:
    try:
        return _grids[grid_id]
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown grid_id: {grid_id}")


def _info(grid_id: str, g: OccupancyGrid) -> GridInfo:
    return GridInfo(
        grid_id=grid_id,
        width=g.width,
        height=g.height,
        resolution=g.resolution,
        origin_x=g.origin_x,
        origin_y=g.origin_y,
    )


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------
@app.post("/grids", response_model=GridInfo, status_code=201)
def create_grid(req: GridCreate) -> GridInfo:
    grid = OccupancyGrid(**req.model_dump())
    grid_id = uuid.uuid4().hex
    _grids[grid_id] = grid
    return _info(grid_id, grid)


@app.get("/grids", response_model=List[GridInfo])
def list_grids() -> List[GridInfo]:
    return [_info(gid, g) for gid, g in _grids.items()]


@app.get("/grids/{grid_id}", response_model=GridInfo)
def get_grid(grid_id: str) -> GridInfo:
    return _info(grid_id, _get_grid(grid_id))


@app.post("/grids/{grid_id}/rays", response_model=UpdateResult)
def integrate_rays(grid_id: str, req: RaysRequest) -> UpdateResult:
    grid = _get_grid(grid_id)
    n = 0
    for ray in req.rays:
        n += grid.integrate_ray(
            ray.sensor_x, ray.sensor_y, ray.end_x, ray.end_y, ray.hit
        )
    return UpdateResult(grid_id=grid_id, cells_updated=n)


@app.post("/grids/{grid_id}/scan", response_model=UpdateResult)
def integrate_scan(grid_id: str, req: ScanRequest) -> UpdateResult:
    grid = _get_grid(grid_id)
    if len(req.angles) != len(req.ranges):
        raise HTTPException(422, "angles and ranges must have equal length")
    n = grid.integrate_scan(
        req.pose_x, req.pose_y, req.pose_theta, req.angles, req.ranges, req.max_range
    )
    return UpdateResult(grid_id=grid_id, cells_updated=n)


@app.get("/grids/{grid_id}/cell")
def get_cell(grid_id: str, ix: int, iy: int) -> dict:
    grid = _get_grid(grid_id)
    if not (0 <= ix < grid.width and 0 <= iy < grid.height):
        raise HTTPException(404, f"cell ({ix}, {iy}) outside grid")
    return {
        "ix": ix,
        "iy": iy,
        "log_odds": float(grid.log_odds[iy, ix]),
        "state": grid.state_at(ix, iy).name,
    }


@app.get("/grids/{grid_id}/log_odds")
def get_log_odds(grid_id: str) -> dict:
    grid = _get_grid(grid_id)
    return {
        "grid_id": grid_id,
        "shape": [grid.height, grid.width],
        "log_odds": grid.log_odds.tolist(),
    }


@app.get("/grids/{grid_id}/probabilities")
def get_probabilities(grid_id: str) -> dict:
    grid = _get_grid(grid_id)
    return {
        "grid_id": grid_id,
        "shape": [grid.height, grid.width],
        "probabilities": grid.probability_grid().tolist(),
    }


@app.post("/grids/{grid_id}/save")
def save(grid_id: str, req: PathRequest) -> dict:
    grid = _get_grid(grid_id)
    path = save_grid(grid, req.path)
    return {"grid_id": grid_id, "path": str(path)}


@app.post("/grids/load", response_model=GridInfo, status_code=201)
def load(req: PathRequest) -> GridInfo:
    if not Path(req.path).is_file():
        raise HTTPException(404, f"no such file: {req.path}")
    grid = load_grid(req.path)
    grid_id = uuid.uuid4().hex
    _grids[grid_id] = grid
    return _info(grid_id, grid)
