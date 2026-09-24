"""FastAPI 后端：加载合成道路图，提供轨迹匹配接口。

启动：uvicorn mapmatching.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .graph import RoadGraph
from .matcher import MatchConfig, Observation, match_trajectory
from .scenarios import build_overpass, build_parallel_roads

app = FastAPI(title="mapmatching", version="0.1.0")

_SCENARIOS = {
    "parallel": build_parallel_roads,
    "overpass": build_overpass,
}
_graphs: dict[str, RoadGraph] = {}


def get_graph(scenario: str) -> RoadGraph:
    if scenario not in _SCENARIOS:
        raise HTTPException(
            status_code=404,
            detail=f"未知场景 {scenario!r}，可选：{sorted(_SCENARIOS)}",
        )
    if scenario not in _graphs:
        _graphs[scenario] = _SCENARIOS[scenario]()
    return _graphs[scenario]


class ObservationIn(BaseModel):
    t: float
    x: float
    y: float


class ConfigIn(BaseModel):
    candidate_radius: float = 50.0
    max_candidates: int = 8
    sigma: float = 10.0
    beta: float = 30.0
    max_gap: float = 60.0
    min_confidence: float = 0.5


class MatchRequest(BaseModel):
    scenario: str = "parallel"
    observations: list[ObservationIn] = Field(min_length=1)
    config: ConfigIn = ConfigIn()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/graph/{scenario}")
def graph_info(scenario: str) -> dict:
    g = get_graph(scenario)
    return {
        "scenario": scenario,
        "nodes": g.nodes,
        "edges": [
            {"id": e.id, "u": e.u, "v": e.v, "length": e.length, "points": e.points.tolist()}
            for e in g.edges
        ],
    }


@app.post("/match")
def match(req: MatchRequest) -> dict:
    g = get_graph(req.scenario)
    cfg = MatchConfig(**req.config.model_dump())
    obs = [Observation(t=o.t, x=o.x, y=o.y) for o in req.observations]
    res = match_trajectory(g, obs, cfg)
    return {
        "points": [
            {
                "t": p.t,
                "x": p.x,
                "y": p.y,
                "matched": p.matched,
                "edge_id": p.edge_id,
                "proj": p.proj,
                "dist_to_edge": p.dist_to_edge,
                "confidence": p.confidence,
                "margin": p.margin,
                "reason": p.reason,
            }
            for p in res.points
        ],
        "unmatched_segments": res.unmatched_segments,
        "segment_breaks": res.segment_breaks,
    }
