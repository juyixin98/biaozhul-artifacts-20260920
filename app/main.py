"""FastAPI service exposing the CBS multi-robot planner.

Run with:  uvicorn app.main:app --port 8000
"""

from __future__ import annotations

from fastapi import FastAPI

from .cbs import solve_cbs, validate_solution
from .grid import GridMap
from .models import SolveRequest, SolveResponse

app = FastAPI(title="multi-robot-cbs", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/solve", response_model=SolveResponse)
def solve(req: SolveRequest) -> SolveResponse:
    errors = _check_request(req)
    if errors:
        return SolveResponse(status="unsolvable", errors=errors)

    grid = GridMap(req.grid.width, req.grid.height, [tuple(o) for o in req.grid.obstacles])
    starts = [tuple(a.start) for a in req.agents]
    goals = [tuple(a.goal) for a in req.agents]

    solution = solve_cbs(grid, starts, goals, max_time=req.max_time)
    if solution is None:
        return SolveResponse(status="unsolvable")

    replay_errors = validate_solution(grid, starts, goals, solution.paths)
    timetable = {
        req.agents[i].id: [[x, y, t] for t, (x, y) in enumerate(solution.paths[i])]
        for i in range(len(req.agents))
    }
    return SolveResponse(
        status="solved",
        cost=solution.cost,
        makespan=solution.makespan,
        timetable=timetable,
        stats=solution.stats.as_dict(),
        errors=replay_errors,
    )


def _check_request(req: SolveRequest) -> list[str]:
    """Validate geometry before planning; returns human-readable errors."""
    errors: list[str] = []
    w, h = req.grid.width, req.grid.height
    obstacles = {tuple(o) for o in req.grid.obstacles}
    for o in obstacles:
        if not (0 <= o[0] < w and 0 <= o[1] < h):
            errors.append(f"obstacle {o} out of bounds")
    ids = [a.id for a in req.agents]
    if len(set(ids)) != len(ids):
        errors.append("duplicate agent ids")
    starts = [tuple(a.start) for a in req.agents]
    goals = [tuple(a.goal) for a in req.agents]
    if len(set(starts)) != len(starts):
        errors.append("two agents share a start cell")
    if len(set(goals)) != len(goals):
        errors.append("two agents share a goal cell")
    for a in req.agents:
        for label, c in (("start", tuple(a.start)), ("goal", tuple(a.goal))):
            if not (0 <= c[0] < w and 0 <= c[1] < h):
                errors.append(f"agent {a.id}: {label} {c} out of bounds")
            elif c in obstacles:
                errors.append(f"agent {a.id}: {label} {c} is an obstacle")
    return errors
