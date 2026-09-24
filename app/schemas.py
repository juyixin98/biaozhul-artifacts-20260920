"""HTTP 协议模型 (Pydantic v2)。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Cell(BaseModel):
    row: int = Field(ge=0)
    col: int = Field(ge=0)


class CreateMapRequest(BaseModel):
    rows: int = Field(ge=1, le=2000)
    cols: int = Field(ge=1, le=2000)
    connectivity: Literal[4, 8] = 8
    # weights[row][col], 省略时全 1.0; 必须全部非负
    weights: list[list[float]] | None = None
    obstacles: list[Cell] | None = None

    @field_validator("weights")
    @classmethod
    def _weights_shape_and_nonneg(cls, v):
        if v is None:
            return v
        width = len(v[0])
        for row in v:
            if len(row) != width:
                raise ValueError("weights 必须是矩形二维数组")
            for x in row:
                if x != x or x == float("inf") or x == float("-inf") or x < 0:
                    raise ValueError("权重必须是非负有限值")
        return v


class Change(BaseModel):
    row: int = Field(ge=0)
    col: int = Field(ge=0)
    kind: Literal["block", "free", "weight"]
    weight: float | None = None

    @field_validator("weight")
    @classmethod
    def _nonneg(cls, v):
        if v is not None:
            if v != v or v == float("inf") or v == float("-inf") or v < 0:
                raise ValueError("权重必须是非负有限值")
        return v


class UpdateMapRequest(BaseModel):
    changes: list[Change] = Field(min_length=1)
    expected_snapshot: str | None = None


class CreatePlannerRequest(BaseModel):
    map_id: str
    start: Cell
    goal: Cell
    snapshot_id: str | None = None


class PlanRequest(BaseModel):
    expected_snapshot: str | None = None


class MoveStartRequest(BaseModel):
    start: Cell
    expected_snapshot: str | None = None


class UpdatePlannerMapRequest(BaseModel):
    changes: list[Change] = Field(min_length=1)
    expected_snapshot: str | None = None


class ResetPlannerRequest(BaseModel):
    snapshot_id: str | None = None
    start: Cell | None = None
