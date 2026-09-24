"""HTTP 协议的 Pydantic 模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


Cell = list[int]


class MapCreate(BaseModel):
    map_id: str = Field(min_length=1, max_length=64, pattern=r"[A-Za-z0-9_-]+")
    width: int = Field(ge=1, le=1000)
    height: int = Field(ge=1, le=1000)
    obstacles: list[Cell] = Field(default_factory=list)


class ObstaclesUpdate(BaseModel):
    obstacles: list[Cell]


class PlanRequest(BaseModel):
    robot_id: int = Field(ge=1, le=8, description="固定优先级：编号越小优先级越高")
    start: Cell | None = Field(
        None, description="[x,y]；机器人已有预订时可省略，沿用其起点")
    goal: Cell
    horizon: int | None = Field(
        None, ge=1, le=64,
        description="规划窗口末端 tick；缺省按已有预订窗口与距离自动确定")
    expected_map_version: int | None = Field(
        None, description="客户端读到的地图版本；不一致返回 MAP_VERSION_MISMATCH")
    expected_reservation_version: int | None = Field(
        None, description="客户端读到的预订版本；陈旧返回 RESERVATION_VERSION_STALE")


class BatchPlanRequest(BaseModel):
    requests: list[PlanRequest] = Field(min_length=1, max_length=8)
    expected_map_version: int | None = None
    expected_reservation_version: int | None = None


class CancelRequest(BaseModel):
    robot_id: int = Field(ge=1, le=8)
    cancel_token: str = Field(min_length=8, max_length=256)


class StandardError(BaseModel):
    error: dict[str, Any]
