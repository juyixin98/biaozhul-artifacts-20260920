"""HMM 轨迹匹配：高斯观测误差 + 沿路转移距离（Newson & Krumm 2009 思路），
Viterbi 求全局最优路径，forward-backward 求后验置信差。

处理的特殊情况：
- 时间断档：相邻观测时间差超过阈值时切成独立片段，断档记录在 time_gaps。
- 不可达候选：候选间沿路不可达时转移概率为 0；某列全部不可达时在该处重启
  Viterbi；观测点没有任何候选边时记为未匹配点。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

import numpy as np
from scipy.special import logsumexp

from .graph import RoadGraph, Candidate


@dataclass
class MatchParams:
    sigma: float = 10.0  # 观测噪声标准差 (m)
    beta: float = 8.0  # 转移距离差异指数分布尺度 (m)
    candidate_radius: float = 50.0  # 候选搜索半径 (m)
    max_time_gap: float = 10.0  # 超过该时间差 (s) 视为断档
    confidence_threshold: float = 0.3  # 后验置信差低于该值时标记低置信


@dataclass
class MatchedPoint:
    index: int
    t: float
    x: float
    y: float
    matched: bool
    edge_id: str | None = None
    fraction: float | None = None
    proj_x: float | None = None
    proj_y: float | None = None
    emission_distance: float | None = None
    confidence_margin: float | None = None  # 最优与次优候选的后验概率差
    low_confidence: bool = False
    segment_id: int = -1
    reason: str | None = None


@dataclass
class TimeGap:
    index: int  # 断档之后第一个观测点下标
    dt: float


@dataclass
class UnmatchedInterval:
    start_index: int
    end_index: int  # 含
    length: int
    reason: str


@dataclass
class MatchResult:
    points: list[MatchedPoint]
    unmatched_intervals: list[UnmatchedInterval]
    time_gaps: list[TimeGap]
    segments: int

    def to_dict(self) -> dict:
        return {
            "points": [asdict(p) for p in self.points],
            "unmatched_intervals": [asdict(u) for u in self.unmatched_intervals],
            "time_gaps": [asdict(g) for g in self.time_gaps],
            "segments": self.segments,
        }


_NEG_INF = -1e300  # 用有限极小值而非真 -inf，方便 logsumexp 数值稳定


class MapMatcher:
    def __init__(self, graph: RoadGraph, params: MatchParams | None = None):
        self.graph = graph
        self.params = params or MatchParams()

    # ------------------------------------------------------------------
    def match(self, ts: list[float], xs: list[float], ys: list[float]) -> MatchResult:
        assert len(ts) == len(xs) == len(ys)
        p = self.params
        n = len(ts)
        results = [None] * n

        # 1) 按时间断档切段
        segments: list[list[int]] = []
        time_gaps: list[TimeGap] = []
        if n > 0:
            cur = [0]
            for i in range(1, n):
                dt = ts[i] - ts[i - 1]
                if dt > p.max_time_gap:
                    segments.append(cur)
                    cur = [i]
                    time_gaps.append(TimeGap(index=i, dt=float(dt)))
                else:
                    cur.append(i)
            segments.append(cur)

        # 2) 每段独立匹配
        for seg_id, idxs in enumerate(segments):
            self._match_segment(idxs, ts, xs, ys, seg_id, results)

        # 3) 汇总未匹配连续区间
        intervals: list[UnmatchedInterval] = []
        i = 0
        while i < n:
            if results[i].matched:
                i += 1
                continue
            j = i
            reason = results[i].reason or "no_candidate"
            while j + 1 < n and not results[j + 1].matched:
                j += 1
            intervals.append(
                UnmatchedInterval(
                    start_index=i,
                    end_index=j,
                    length=j - i + 1,
                    reason=reason,
                )
            )
            i = j + 1

        return MatchResult(
            points=results,
            unmatched_intervals=intervals,
            time_gaps=time_gaps,
            segments=len(segments),
        )

    # ------------------------------------------------------------------
    def _match_segment(
        self,
        idxs: list[int],
        ts: list[float],
        xs: list[float],
        ys: list[float],
        seg_id: int,
        out: list,
    ) -> None:
        p = self.params

        # 候选生成
        cands: list[list[Candidate]] = []
        for i in idxs:
            cands.append(self.graph.candidates(xs[i], ys[i], p.candidate_radius))

        # 观测（发射）对数概率
        log_emit = []
        for col in cands:
            if col:
                d = np.array([c.distance for c in col])
                le = -0.5 * (d / p.sigma) ** 2  # 高斯，归一化常数各候选相同可省略
                log_emit.append(le)
            else:
                log_emit.append(np.array([]))

        # 转移对数概率（不可达 = _NEG_INF），并在“全列不可达”处重启
        log_trans: list[np.ndarray | None] = [None]
        restarts: set[int] = set()
        for t in range(len(idxs) - 1):
            ca, cb = cands[t], cands[t + 1]
            gps_d = math.hypot(xs[idxs[t + 1]] - xs[idxs[t]], ys[idxs[t + 1]] - ys[idxs[t]])
            mat = np.full((len(ca), len(cb)), _NEG_INF)
            for ii, a in enumerate(ca):
                for jj, b in enumerate(cb):
                    rd = self.graph.route_distance(a, b)
                    if np.isfinite(rd):
                        mat[ii, jj] = -abs(rd - gps_d) / p.beta
            if len(ca) > 0 and len(cb) > 0 and not np.any(mat > _NEG_INF / 2):
                # 下一列所有候选都不可达：在 t+1 处重启（允许匹配，但不继承路径）
                restarts.add(t + 1)
                mat = np.zeros((len(ca), len(cb)))
            log_trans.append(mat)

        # Viterbi + forward-backward（按重启点和无候选列切成子段）
        empty_cols = {t for t, col in enumerate(cands) if not col}
        bounds = sorted(
            {0, len(idxs)} | restarts | empty_cols | {t + 1 for t in empty_cols}
        )
        best_path: dict[int, int] = {}
        posterior_margin: dict[int, float] = {}
        for s0, s1 in zip(bounds[:-1], bounds[1:]):
            if s1 <= s0:
                continue
            self._viterbi(cands, log_emit, log_trans, s0, s1, best_path)
            self._posterior_margin(cands, log_emit, log_trans, s0, s1, posterior_margin)

        # 回填结果
        for local, gi in enumerate(idxs):
            if not cands[local]:
                out[gi] = MatchedPoint(
                    index=gi,
                    t=ts[gi],
                    x=xs[gi],
                    y=ys[gi],
                    matched=False,
                    segment_id=seg_id,
                    reason="no_candidate",
                )
                continue
            k = best_path.get(local)
            if k is None:
                out[gi] = MatchedPoint(
                    index=gi,
                    t=ts[gi],
                    x=xs[gi],
                    y=ys[gi],
                    matched=False,
                    segment_id=seg_id,
                    reason="no_reachable_path",
                )
                continue
            c = cands[local][k]
            margin = posterior_margin.get(local, 1.0)
            out[gi] = MatchedPoint(
                index=gi,
                t=ts[gi],
                x=xs[gi],
                y=ys[gi],
                matched=True,
                edge_id=c.edge_id,
                fraction=float(c.fraction),
                proj_x=float(c.proj_x),
                proj_y=float(c.proj_y),
                emission_distance=float(c.distance),
                confidence_margin=float(margin),
                low_confidence=margin < p.confidence_threshold,
                segment_id=seg_id,
            )

    # ------------------------------------------------------------------
    @staticmethod
    def _viterbi(cands, log_emit, log_trans, s0, s1, best_path):
        K = [len(c) for c in cands[s0:s1]]
        if any(k == 0 for k in K):
            return  # 不应发生（无候选的列已单独处理），保险起见
        delta = log_emit[s0].copy()
        back = np.full((s1 - s0, max(K)), -1, dtype=int)
        for t in range(s0 + 1, s1):
            scores = delta[:, None] + log_trans[t]
            bp = np.argmax(scores, axis=0)
            delta = scores[bp, np.arange(K[t - s0])] + log_emit[t]
            back[t - s0, : K[t - s0]] = bp
        k = int(np.argmax(delta))
        best_path[s1 - 1] = k
        for t in range(s1 - 1, s0, -1):
            k = int(back[t - s0, k])
            best_path[t - 1] = k

    @staticmethod
    def _posterior_margin(cands, log_emit, log_trans, s0, s1, margin_out):
        T = s1 - s0
        K = [len(c) for c in cands[s0:s1]]
        if any(k == 0 for k in K):
            return  # 无候选列不参与后验计算
        log_alpha = [np.zeros(k) for k in K]
        log_alpha[0] = log_emit[s0].copy()
        for t in range(1, T):
            trans = log_trans[s0 + t]
            log_alpha[t] = log_emit[s0 + t] + logsumexp(
                log_alpha[t - 1][:, None] + trans, axis=0
            )
        log_beta = [np.zeros(k) for k in K]
        for t in range(T - 2, -1, -1):
            trans = log_trans[s0 + t + 1]
            log_beta[t] = logsumexp(
                trans + log_emit[s0 + t + 1][None, :] + log_beta[t + 1][None, :],
                axis=1,
            )
        for t in range(T):
            log_gamma = log_alpha[t] + log_beta[t]
            gamma = np.exp(log_gamma - logsumexp(log_gamma))
            top = np.sort(gamma)[::-1]
            margin_out[s0 + t] = float(top[0] - (top[1] if len(top) > 1 else 0.0))
