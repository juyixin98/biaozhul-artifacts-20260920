"""FastAPI 入口。

启动：
    uvicorn app.main:app --reload
环境变量：
    STP_DB_PATH  SQLite 文件路径（默认 stp.db）
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .database import Database
from .errors import ServiceError
from .schemas import BatchPlanRequest, CancelRequest, MapCreate, ObstaclesUpdate, PlanRequest
from .service import PlannerService

_db: Database | None = None
_service: PlannerService | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database(os.environ.get("STP_DB_PATH", "stp.db"))
    return _db


def get_service() -> PlannerService:
    global _service
    if _service is None:
        _service = PlannerService(get_db())
    return _service


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_service()  # 启动时即初始化
    yield
    if _db is not None:
        _db.close()


app = FastAPI(
    title="时空预约规划 Spatio-Temporal Reservation Planner",
    version=__version__,
    description="小规模机器人（<=8）时空栅格预约服务：时空 A* + 顶点/边冲突 "
                "检测 + 终点占用 + 地图/预订乐观版本控制。",
    lifespan=lifespan,
)


@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError):
    return JSONResponse(
        status_code=exc.http_status,
        content={"error": {"code": exc.code, "message": exc.message,
                             "details": exc.details}},
    )


@app.get("/health")
def health():
    s = get_service()
    with s.db.write_lock:
        m = s.db.conn.execute(
            "SELECT COUNT(*) AS n FROM maps").fetchone()["n"]
    return {"status": "ok", "version": __version__, "maps": m}


@app.post("/api/maps", status_code=201)
def create_map(body: MapCreate):
    return get_service().create_map(
        body.map_id, body.width, body.height, body.obstacles)


@app.get("/api/maps/{map_id}")
def get_map(map_id: str):
    return get_service().get_map(map_id)


@app.put("/api/maps/{map_id}/obstacles")
def update_obstacles(map_id: str, body: ObstaclesUpdate):
    return get_service().update_obstacles(map_id, body.obstacles)


@app.post("/api/maps/{map_id}/reservations")
def plan_reservation(map_id: str, body: PlanRequest):
    return get_service().plan_one(
        map_id,
        body.robot_id,
        body.start,
        body.goal,
        body.horizon,
        body.expected_map_version,
        body.expected_reservation_version,
    )


@app.post("/api/maps/{map_id}/reservations/batch")
def batch_plan(map_id: str, body: BatchPlanRequest):
    reqs = [
        {
            "robot_id": r.robot_id,
            "start": r.start,
            "goal": r.goal,
            "horizon": r.horizon,
        }
        for r in body.requests
    ]
    return get_service().batch_plan(
        map_id, reqs, body.expected_map_version,
        body.expected_reservation_version)


@app.get("/api/maps/{map_id}/reservations")
def list_reservations(map_id: str):
    return get_service().list_reservations(map_id)


@app.post("/api/maps/{map_id}/reservations/cancel")
def cancel(map_id: str, body: CancelRequest):
    return get_service().cancel(map_id, body.robot_id, body.cancel_token)


@app.get("/api/maps/{map_id}/debug/state")
def debug_state(map_id: str):
    return get_service().debug_state(map_id)
