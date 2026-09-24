"""候选边生成：按点到边折线的垂直距离筛选，并计算沿边投影位置。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .graph import RoadGraph


@dataclass(frozen=True)
class Candidate:
    """一个观测点的候选匹配：边 + 投影信息。

    edge_id: 候选边
    dist: 观测点到边的垂直距离（米）
    along: 投影点沿边起点的弧长（米）
    proj: 投影点坐标 (x, y)
    """

    edge_id: str
    dist: float
    along: float
    proj: tuple[float, float]


def _project_to_polyline(p: np.ndarray, pts: np.ndarray) -> tuple[float, float, np.ndarray]:
    """点 p 到折线 pts 的最近距离、最近点沿折线弧长、最近点坐标。"""
    seg = np.diff(pts, axis=0)
    seg_len = np.hypot(seg[:, 0], seg[:, 1])
    seg_len_safe = np.where(seg_len < 1e-12, 1e-12, seg_len)
    # p 在每条线段上的投影参数 t ∈ [0, 1]
    ap = p[None, :] - pts[:-1]
    t = np.sum(ap * seg, axis=1) / (seg_len_safe**2)
    t = np.clip(t, 0.0, 1.0)
    closest = pts[:-1] + t[:, None] * seg
    d = np.hypot(closest[:, 0] - p[0], closest[:, 1] - p[1])
    i = int(np.argmin(d))
    along = float(np.sum(seg_len[:i]) + t[i] * seg_len[i])
    return float(d[i]), along, closest[i]


def generate_candidates(
    graph: RoadGraph, point: tuple[float, float], radius: float, max_candidates: int = 8
) -> list[Candidate]:
    """生成 point 的候选边集合：距离不超过 radius 的边，按距离升序，最多 max_candidates 条。"""
    p = np.asarray(point, dtype=float)
    cands: list[Candidate] = []
    for e in graph.edges:
        d, along, proj = _project_to_polyline(p, e.points)
        if d <= radius:
            cands.append(
                Candidate(edge_id=e.id, dist=d, along=along, proj=(float(proj[0]), float(proj[1])))
            )
    cands.sort(key=lambda c: c.dist)
    return cands[:max_candidates]
