"""FastAPI 应用与路由。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .schemas import (
    CreateMapRequest,
    CreatePlannerRequest,
    MoveStartRequest,
    PlanRequest,
    ResetPlannerRequest,
    UpdateMapRequest,
    UpdatePlannerMapRequest,
)
from .service import ApiError, PlannerService

app = FastAPI(
    title="增量路径修复服务",
    version="1.0.0",
    description="二维加权栅格上的 D* Lite 增量规划, 地图修改与快照绑定, "
    "每次结果与独立 Dijkstra 比较最优成本。",
)
service = PlannerService()


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.get("/health")
def health():
    return {"status": "ok", "maps": len(service.maps), "planners": len(service.planners)}


# ------------------------- 地图 ------------------------- #
@app.post("/api/maps")
def create_map(req: CreateMapRequest):
    obstacles = [[c.row, c.col] for c in (req.obstacles or [])]
    map_id, snap = service.create_map(
        rows=req.rows,
        cols=req.cols,
        weights=req.weights,
        obstacles=obstacles,
        connectivity=req.connectivity,
    )
    return {
        "map_id": map_id,
        "rows": req.rows,
        "cols": req.cols,
        "connectivity": req.connectivity,
        "snapshot": snap.to_info(),
    }


@app.get("/api/maps/{map_id}")
def get_map(map_id: str):
    return service.get_map_info(map_id)


@app.post("/api/maps/{map_id}/updates")
def update_map(map_id: str, req: UpdateMapRequest):
    snap = service.update_map(
        map_id,
        changes=[c.model_dump() for c in req.changes],
        expected_snapshot=req.expected_snapshot,
    )
    return {"snapshot": snap.to_info()}


@app.get("/api/maps/{map_id}/verify")
def verify_map(map_id: str):
    return service.verify_map(map_id)


# ------------------------- 规划器 ------------------------- #
@app.post("/api/planners")
def create_planner(req: CreatePlannerRequest):
    rec = service.create_planner(
        map_id=req.map_id,
        start=(req.start.row, req.start.col),
        goal=(req.goal.row, req.goal.col),
        snapshot_id=req.snapshot_id,
    )
    return service.get_planner_info(rec.planner_id)


@app.get("/api/planners/{planner_id}")
def get_planner(planner_id: str):
    return service.get_planner_info(planner_id)


@app.post("/api/planners/{planner_id}/plan")
def plan(planner_id: str, req: PlanRequest):
    return service.plan(planner_id, req.expected_snapshot)


@app.post("/api/planners/{planner_id}/move-start")
def move_start(planner_id: str, req: MoveStartRequest):
    return service.move_start(
        planner_id, (req.start.row, req.start.col), req.expected_snapshot
    )


@app.post("/api/planners/{planner_id}/update-map")
def update_planner_map(planner_id: str, req: UpdatePlannerMapRequest):
    return service.update_planner_map(
        planner_id,
        changes=[c.model_dump() for c in req.changes],
        expected_snapshot=req.expected_snapshot,
    )


@app.post("/api/planners/{planner_id}/reset")
def reset_planner(planner_id: str, req: ResetPlannerRequest):
    return service.reset_planner(
        planner_id,
        snapshot_id=req.snapshot_id,
        new_start=(req.start.row, req.start.col) if req.start else None,
    )
