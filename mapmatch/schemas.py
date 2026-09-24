"""FastAPI 请求/响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class NodeIn(BaseModel):
    id: str
    x: float
    y: float


class EdgeIn(BaseModel):
    id: str
    u: str
    v: str
    polyline: list[list[float]] = Field(..., description="折线坐标 [[x,y],...]，首末点须与 u/v 节点一致")


class GraphIn(BaseModel):
    nodes: list[NodeIn]
    edges: list[EdgeIn]


class PointIn(BaseModel):
    t: float
    x: float
    y: float


class ParamsIn(BaseModel):
    sigma: float = 10.0
    beta: float = 8.0
    candidate_radius: float = 50.0
    max_time_gap: float = 10.0
    confidence_threshold: float = 0.3


class MatchRequest(BaseModel):
    graph: GraphIn
    trajectory: list[PointIn]
    params: ParamsIn = ParamsIn()


class MatchResponse(BaseModel):
    points: list[dict]
    unmatched_intervals: list[dict]
    time_gaps: list[dict]
    segments: int
