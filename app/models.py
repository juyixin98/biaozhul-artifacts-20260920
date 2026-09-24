"""Pydantic models for the incremental path repair HTTP API."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

Coord = tuple[int, int]


class CreateMapRequest(BaseModel):
    width: int = Field(..., gt=0, le=4096)
    height: int = Field(..., gt=0, le=4096)
    # Optional full matrices; absent means all cells cost 1 / free.
    cost: Optional[List[List[float]]] = None
    blocked: Optional[List[List[bool]]] = None
    start: Coord
    goal: Coord
    connectivity: int = 8
    diagonal_rule: str = "two_blocked"

    @field_validator("connectivity")
    @classmethod
    def _conn(cls, v: int) -> int:
        if v not in (4, 8):
            raise ValueError("connectivity must be 4 or 8")
        return v

    @field_validator("diagonal_rule")
    @classmethod
    def _rule(cls, v: str) -> str:
        if v != "two_blocked":
            raise ValueError("only the 'two_blocked' diagonal rule is supported")
        return v


class CellCost(BaseModel):
    x: int = Field(..., ge=0)
    y: int = Field(..., ge=0)
    # weight paid to enter the cell; null marks the cell blocked.
    cost: Optional[float] = None
    blocked: Optional[bool] = None

    @field_validator("cost")
    @classmethod
    def _cost(cls, v: Optional[float]) -> Optional[float]:
        if v is not None:
            if v != v or v in (float("inf"), float("-inf")):
                raise ValueError("cost must be finite")
            if v < 0:
                raise ValueError("negative costs are forbidden")
        return v


class UpdateCostsRequest(BaseModel):
    map_id: str
    expected_snapshot_id: str
    updates: List[CellCost] = Field(default_factory=list)
    auto_replan: bool = True


class MoveStartRequest(BaseModel):
    map_id: str
    expected_snapshot_id: str
    start: Coord
    auto_replan: bool = True


class PlanRequest(BaseModel):
    map_id: str
    expected_snapshot_id: Optional[str] = None


class VerifySignatureRequest(BaseModel):
    map_id: str
    snapshot_id: str
    body: dict
    signature: str


class PathResponse(BaseModel):
    map_id: str
    revision: int
    snapshot_id: str
    start: Coord
    goal: Coord
    connectivity: int
    reachable: bool
    # null cost/path when the goal is unreachable
    cost: Optional[float]
    path: Optional[List[Coord]]
    path_cost_check: Optional[float] = None
    path_valid: Optional[bool] = None
    dijkstra_cost: Optional[float]
    optimal_match: bool
    # diagnostics, not guarantees
    reexpanded_nodes: int
    vertex_updates: int
    dijkstra_settled_nodes: int
    open_queue_size: int
    km: float
    map_changed: bool = False
    signature: Optional[str] = None


class SnapshotResponse(BaseModel):
    map_id: str
    revision: int
    snapshot_id: str
    width: int
    height: int
    start: Coord
    goal: Coord
    connectivity: int
    diagonal_rule: str
    hmac_key: str  # returned once at creation so clients can verify signatures


class SimpleAck(BaseModel):
    map_id: str
    deleted: bool
