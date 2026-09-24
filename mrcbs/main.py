"""FastAPI 服务：多机器人时空避碰规划接口。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .brute_force import brute_force_solve
from .cbs import pad_timetable, solve_cbs
from .grid import Cell, parse_grid
from .validate import validate_timetable

app = FastAPI(title="多机器人时空避碰规划服务", version="0.1.0")


class AgentSpec(BaseModel):
    start: tuple[int, int]
    goal: tuple[int, int]


class SolveRequest(BaseModel):
    grid: list[str] = Field(..., description="网格行，'.' 可通行，'#' 障碍")
    agents: list[AgentSpec]
    max_nodes: int = Field(10000, ge=1, le=1000000, description="CBS 约束树扩展节点上限")
    check_brute_force: bool = Field(False, description="是否同时用穷举核对最优性（仅小规模）")


class SolveResponse(BaseModel):
    status: str
    cost: int | None
    makespan: int | None
    timetable: list[list[tuple[int, int]]] | None
    stats: dict
    brute_force_cost: int | None = None
    optimal_verified: bool | None = None


class ValidateRequest(BaseModel):
    grid: list[str]
    agents: list[AgentSpec]
    timetable: list[list[tuple[int, int]]]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/solve", response_model=SolveResponse)
def solve(req: SolveRequest) -> SolveResponse:
    try:
        grid = parse_grid(req.grid)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    starts: list[Cell] = [tuple(a.start) for a in req.agents]  # type: ignore[misc]
    goals: list[Cell] = [tuple(a.goal) for a in req.agents]  # type: ignore[misc]
    if not starts:
        raise HTTPException(status_code=422, detail="agents 不能为空")

    result = solve_cbs(grid, starts, goals, max_nodes=req.max_nodes)
    timetable = pad_timetable(result.paths) if result.paths else None
    makespan = (len(timetable[0]) - 1) if timetable else None

    bf_cost = None
    verified = None
    if req.check_brute_force:
        bf = brute_force_solve(grid, starts, goals)
        if bf is not None:
            bf_cost = bf[0]
            verified = (result.cost == bf_cost) if result.cost is not None else False
        elif result.status == "unsolvable":
            verified = True  # 穷举同样无解

    return SolveResponse(
        status=result.status,
        cost=result.cost,
        makespan=makespan,
        timetable=timetable,
        stats=result.stats,
        brute_force_cost=bf_cost,
        optimal_verified=verified,
    )


@app.post("/validate")
def validate(req: ValidateRequest) -> dict:
    try:
        grid = parse_grid(req.grid)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    starts: list[Cell] = [tuple(a.start) for a in req.agents]  # type: ignore[misc]
    goals: list[Cell] = [tuple(a.goal) for a in req.agents]  # type: ignore[misc]
    timetable: list[list[Cell]] = [[tuple(c) for c in path] for path in req.timetable]  # type: ignore[misc]
    violations = validate_timetable(grid, starts, goals, timetable)
    return {"valid": not violations, "violations": violations}
