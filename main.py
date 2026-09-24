"""FastAPI 入口：POST /match 执行轨迹匹配，GET /health 健康检查。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from mapmatch.graph import Node, Edge, RoadGraph
from mapmatch.matcher import MapMatcher, MatchParams
from mapmatch.schemas import MatchRequest, MatchResponse

app = FastAPI(title="mapmatch", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/match", response_model=MatchResponse)
def match(req: MatchRequest):
    if not req.trajectory:
        raise HTTPException(status_code=400, detail="trajectory is empty")
    try:
        graph = RoadGraph(
            nodes=[Node(n.id, n.x, n.y) for n in req.graph.nodes],
            edges=[
                Edge(e.id, e.u, e.v, tuple(tuple(p) for p in e.polyline))
                for e in req.graph.edges
            ],
        )
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"bad graph: {exc}") from exc
    params = MatchParams(**req.params.model_dump())
    matcher = MapMatcher(graph, params)
    ts = [p.t for p in req.trajectory]
    xs = [p.x for p in req.trajectory]
    ys = [p.y for p in req.trajectory]
    result = matcher.match(ts, xs, ys)
    return MatchResponse(**result.to_dict())
