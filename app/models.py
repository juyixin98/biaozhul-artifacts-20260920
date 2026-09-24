"""Pydantic request/response schemas for the planning service."""

from __future__ import annotations

from pydantic import BaseModel, Field


class GridSpec(BaseModel):
    width: int = Field(gt=0, le=64)
    height: int = Field(gt=0, le=64)
    obstacles: list[tuple[int, int]] = []


class AgentSpec(BaseModel):
    id: str
    start: tuple[int, int]
    goal: tuple[int, int]


class SolveRequest(BaseModel):
    grid: GridSpec
    agents: list[AgentSpec] = Field(min_length=1, max_length=8)
    max_time: int | None = Field(default=None, gt=0)


class SolveResponse(BaseModel):
    status: str  # "solved" | "unsolvable"
    cost: int | None = None            # sum of arrival times
    makespan: int | None = None
    # timetable[agent_id] = [[x, y, t], ...] — a replayable schedule
    timetable: dict[str, list[list[int]]] = {}
    stats: dict[str, int] = {}
    errors: list[str] = []
