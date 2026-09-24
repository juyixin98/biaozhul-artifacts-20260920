"""匹配编排：候选生成 → 分段（时间断档）→ Viterbi → 置信度与未匹配段输出。"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .candidates import Candidate, generate_candidates
from .graph import RoadGraph
from .hmm import (
    emission_logprob,
    forward_backward,
    route_distance,
    transition_logprob,
    viterbi,
)


@dataclass(frozen=True)
class Observation:
    """一个轨迹观测点。t 单位秒，x/y 单位米（局部平面坐标）。"""

    t: float
    x: float
    y: float


@dataclass
class MatchConfig:
    candidate_radius: float = 50.0  # 候选边搜索半径（米）
    max_candidates: int = 8
    sigma: float = 10.0  # 观测噪声标准差（米）
    beta: float = 30.0  # 转移概率尺度参数（米）
    max_gap: float = 60.0  # 相邻观测时间差超过该值（秒）视为断档，切分段落
    min_confidence: float = 0.5  # 后验概率低于该值视为未匹配


@dataclass
class MatchedPoint:
    t: float
    x: float
    y: float
    matched: bool
    edge_id: str | None = None
    proj: tuple[float, float] | None = None
    dist_to_edge: float | None = None
    confidence: float = 0.0  # 所选候选的后验概率
    margin: float = 0.0  # 置信差：最优与次优候选后验概率之差
    reason: str | None = None  # 未匹配原因


@dataclass
class MatchResult:
    points: list[MatchedPoint] = field(default_factory=list)
    unmatched_segments: list[tuple[int, int]] = field(default_factory=list)  # [start, end] 下标闭区间
    segment_breaks: list[int] = field(default_factory=list)  # 时间断档发生处的观测下标（断档后一点）


def _match_segment(
    graph: RoadGraph,
    obs: list[Observation],
    candidates: list[list[Candidate]],
    cfg: MatchConfig,
) -> list[MatchedPoint]:
    """对一段连续观测做 Viterbi 匹配并计算后验置信度。"""
    n = len(obs)
    results: list[MatchedPoint] = []
    valid_idx = [i for i in range(n) if candidates[i]]

    # 无候选的点直接标记未匹配
    for i in range(n):
        if not candidates[i]:
            results.append(
                MatchedPoint(t=obs[i].t, x=obs[i].x, y=obs[i].y, matched=False, reason="no_candidate")
            )
        else:
            results.append(None)  # 占位，稍后填充

    if not valid_idx:
        return results

    # 在有效点上运行 HMM（无候选点被跳过，其前后点直接转移）
    emission: list[np.ndarray] = []
    transition: list[np.ndarray] = []
    for i in valid_idx:
        emission.append(
            np.array([emission_logprob(c.dist, cfg.sigma) for c in candidates[i]])
        )
    for a_i, b_i in zip(valid_idx[:-1], valid_idx[1:]):
        ca, cb = candidates[a_i], candidates[b_i]
        linear = float(np.hypot(obs[b_i].x - obs[a_i].x, obs[b_i].y - obs[a_i].y))
        trans = np.zeros((len(ca), len(cb)))
        for p, cand_a in enumerate(ca):
            for q, cand_b in enumerate(cb):
                rd = route_distance(graph, cand_a, cand_b)
                trans[p, q] = transition_logprob(rd, linear, cfg.beta)
        transition.append(trans)

    path, _ = viterbi(emission, transition)
    posterior = forward_backward(emission, transition)

    for k, i in enumerate(valid_idx):
        chosen = path[k]
        post = posterior[k]
        conf = float(post[chosen])
        sorted_post = np.sort(post)[::-1]
        margin = float(sorted_post[0] - sorted_post[1]) if len(sorted_post) > 1 else 1.0
        cand = candidates[i][chosen]
        matched = conf >= cfg.min_confidence
        results[i] = MatchedPoint(
            t=obs[i].t,
            x=obs[i].x,
            y=obs[i].y,
            matched=matched,
            edge_id=cand.edge_id if matched else None,
            proj=cand.proj if matched else None,
            dist_to_edge=cand.dist,
            confidence=conf,
            margin=margin,
            reason=None if matched else "low_confidence",
        )
    return results


def match_trajectory(
    graph: RoadGraph, observations: list[Observation], cfg: MatchConfig | None = None
) -> MatchResult:
    """匹配整条轨迹，返回逐点结果与未匹配段。"""
    cfg = cfg or MatchConfig()
    result = MatchResult()
    if not observations:
        return result

    # 按时间排序并切分时间断档
    obs = sorted(observations, key=lambda o: o.t)
    segments: list[tuple[int, int]] = []  # [start, end) 观测下标
    start = 0
    for i in range(1, len(obs)):
        if obs[i].t - obs[i - 1].t > cfg.max_gap:
            segments.append((start, i))
            result.segment_breaks.append(i)
            start = i
    segments.append((start, len(obs)))

    all_points: list[MatchedPoint | None] = [None] * len(obs)
    for s, e in segments:
        seg_obs = obs[s:e]
        seg_cands = [
            generate_candidates(graph, (o.x, o.y), cfg.candidate_radius, cfg.max_candidates)
            for o in seg_obs
        ]
        seg_points = _match_segment(graph, seg_obs, seg_cands, cfg)
        for j, p in enumerate(seg_points):
            all_points[s + j] = p

    result.points = [p for p in all_points]  # type: ignore[list-item]

    # 汇总未匹配段（连续未匹配点的下标区间）
    i = 0
    while i < len(result.points):
        if not result.points[i].matched:
            j = i
            while j + 1 < len(result.points) and not result.points[j + 1].matched:
                j += 1
            result.unmatched_segments.append((i, j))
            i = j + 1
        else:
            i += 1
    return result
