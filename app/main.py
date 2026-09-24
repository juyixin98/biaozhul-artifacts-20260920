"""FastAPI 入口：时空预约规划 HTTP API。

启动
====
    uvicorn app.main:app --host 127.0.0.1 --port 8000

数据库路径由环境变量 ``SPATIO_DB`` 指定，默认 ``./spatio.db``。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import db as dbmod
from . import service
from .service import ServiceError

DB_PATH = os.environ.get("SPATIO_DB", os.path.join(os.getcwd(), "spatio.db"))


def conn_factory():
    return dbmod.connect(DB_PATH)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    conn = conn_factory()
    try:
        dbmod.init_db(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="时空预约规划 (Spatio-Temporal Reservation Planner)",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "小规模机器人（最多 8 个）时空栅格预约服务。动作仅限等待或移动一格；"
        "规划同时禁止同刻占同格（顶点冲突）与对向交换（边冲突）；固定优先级、"
        "贪婪非完备。预订携带地图版本与预约版本，提交时重新校验。"
    ),
)


@app.exception_handler(ServiceError)
async def service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
        },
    )


# ---------------------------------------------------------------------- schemas


class CellIn(BaseModel):
    x: int
    y: int


class PlanSingleIn(BaseModel):
    robot_id: str = Field(..., description="机器人 id，如 R1")
    goal: CellIn = Field(..., description="目标格")
    expected_map_version: Optional[int] = Field(
        None, description="客户端预期的地图版本（乐观锁）"
    )
    expected_reservation_version: Optional[int] = Field(
        None, description="客户端预期的预约版本（乐观锁）"
    )
    horizon: int = Field(service.DEFAULT_HORIZON, ge=1, le=2000)
    max_nodes: int = Field(service.DEFAULT_MAX_NODES, ge=1, le=2_000_000)


class GoalIn(BaseModel):
    goal: CellIn


class PlanBatchIn(BaseModel):
    goals: Dict[str, GoalIn] = Field(
        ..., description="机器人 id -> 目标；规划优先级固定为 id 升序"
    )
    expected_map_version: Optional[int] = None
    expected_reservation_version: Optional[int] = None
    horizon: int = Field(service.DEFAULT_HORIZON, ge=1, le=2000)
    max_nodes: int = Field(service.DEFAULT_MAX_NODES, ge=1, le=2_000_000)


class MapIn(BaseModel):
    width: int = Field(..., ge=1, le=1000)
    height: int = Field(..., ge=1, le=1000)
    obstacles: List[CellIn] = Field(default_factory=list)
    expected_map_version: Optional[int] = None


class RobotIn(BaseModel):
    start: CellIn


class CancelIn(BaseModel):
    cancel_token: str


# ---------------------------------------------------------------------- routes


@app.get("/health", tags=["meta"])
def health() -> dict:
    conn = conn_factory()
    try:
        dbmod.init_db(conn)
        return {"status": "ok", "db": DB_PATH}
    finally:
        conn.close()


@app.get("/state", tags=["state"])
def state() -> dict:
    return service.get_state(conn_factory)


@app.post("/admin/reset", tags=["admin"])
def admin_reset() -> dict:
    return service.reset(conn_factory)


@app.put("/map", tags=["map"])
def put_map(body: MapIn) -> dict:
    return service.update_map(
        conn_factory,
        width=body.width,
        height=body.height,
        obstacles=[(o.x, o.y) for o in body.obstacles],
        expected_map_version=body.expected_map_version,
    )


@app.put("/robots/{robot_id}", tags=["robots"])
def put_robot(robot_id: str, body: RobotIn) -> dict:
    return service.register_robot(
        conn_factory, robot_id=robot_id, start=(body.start.x, body.start.y)
    )


@app.post("/reservations/plan", tags=["reservations"])
def plan_single_route(body: PlanSingleIn) -> dict:
    return service.plan_single(
        conn_factory,
        robot_id=body.robot_id,
        goal=(body.goal.x, body.goal.y),
        expected_map_version=body.expected_map_version,
        expected_resv_version=body.expected_reservation_version,
        horizon=body.horizon,
        max_nodes=body.max_nodes,
    )


@app.post("/reservations/plan-batch", tags=["reservations"])
def plan_batch_route(body: PlanBatchIn) -> dict:
    return service.plan_batch(
        conn_factory,
        goals={rid: (g.goal.x, g.goal.y) for rid, g in body.goals.items()},
        expected_map_version=body.expected_map_version,
        expected_resv_version=body.expected_reservation_version,
        horizon=body.horizon,
        max_nodes=body.max_nodes,
    )


@app.post("/reservations/{resv_id}/cancel", tags=["reservations"])
def cancel_route(resv_id: str, body: CancelIn) -> dict:
    return service.cancel_reservation(
        conn_factory, resv_id=resv_id, token=body.cancel_token
    )
