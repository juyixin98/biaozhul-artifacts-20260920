"""HMM 匹配核心：发射/转移概率、Viterbi 解码、前向-后验置信度。

模型（Newson & Krumm 2009 的常用形式）：
- 发射概率：观测点到候选边的垂直距离 d ~ N(0, sigma)，取对数得 -d^2/(2 sigma^2)（常数项省略）。
- 转移概率：| 路网路径距离 - 观测直线距离 | 越小越可能，p ∝ exp(-|Δ| / beta)。
  路网距离 = 前一候选沿边剩余长度 + 节点间最短路 + 后一候选沿边弧长；
  不可达（最短路为 inf）的候选对转移概率为 0（对数概率 -inf），
  因此 Viterbi 不会在没有路网连接的两条边之间跳转。
"""

from __future__ import annotations

import numpy as np

from .candidates import Candidate
from .graph import RoadGraph

NEG_INF = -np.inf


def emission_logprob(dist: float, sigma: float) -> float:
    """发射对数概率（省略归一化常数，不影响相对比较）。"""
    return -(dist * dist) / (2.0 * sigma * sigma)


def route_distance(graph: RoadGraph, a: Candidate, b: Candidate) -> float:
    """候选 a 投影点 → 候选 b 投影点 的沿路网距离；不可达返回 np.inf。"""
    ea = graph.edge_by_id[a.edge_id]
    eb = graph.edge_by_id[b.edge_id]
    if a.edge_id == b.edge_id:
        if b.along >= a.along:
            return b.along - a.along
        # 同边反向：需要绕路网一圈（从 a 投影到边末，再到边首，再到 b 投影）
        tail = ea.length - a.along
        loop = graph.path_distance(ea.v, ea.u)
        if not np.isfinite(loop):
            return np.inf
        return tail + loop + b.along
    tail = ea.length - a.along
    mid = graph.path_distance(ea.v, eb.u)
    if not np.isfinite(mid):
        return np.inf
    return tail + mid + b.along


def transition_logprob(route_dist: float, linear_dist: float, beta: float) -> float:
    """转移对数概率；不可达返回 -inf。"""
    if not np.isfinite(route_dist):
        return NEG_INF
    return -abs(route_dist - linear_dist) / beta


def viterbi(
    emission: list[np.ndarray], transition: list[np.ndarray]
) -> tuple[list[int], float]:
    """标准对数空间 Viterbi。

    emission[t]: (n_t,) 各候选发射对数概率
    transition[t]: (n_t, n_{t+1}) 从时刻 t 到 t+1 的转移对数概率（共 T-1 个）
    返回每个时刻的最优候选下标与最优路径对数概率。
    若某条路径全不可达，对应状态自然被淘汰；若整行不可达则该时刻回退为
    发射概率最大的候选（由调用方结合置信度标记为低置信）。
    """
    T = len(emission)
    delta = [None] * T
    psi = [None] * T
    delta[0] = emission[0].astype(float).copy()
    psi[0] = np.zeros(len(emission[0]), dtype=int)
    for t in range(1, T):
        trans = transition[t - 1]  # (n_prev, n_cur)
        score = delta[t - 1][:, None] + trans  # (n_prev, n_cur)
        best_prev = np.argmax(score, axis=0)
        best_score = score[best_prev, np.arange(score.shape[1])]
        delta[t] = best_score + emission[t]
        psi[t] = best_prev
    path = [0] * T
    path[T - 1] = int(np.argmax(delta[T - 1]))
    best_logprob = float(delta[T - 1][path[T - 1]])
    for t in range(T - 1, 0, -1):
        path[t - 1] = int(psi[t][path[t]])
    return path, best_logprob


def forward_backward(
    emission: list[np.ndarray], transition: list[np.ndarray]
) -> list[np.ndarray]:
    """前向-后向算法，返回每个时刻各候选的后验概率（归一化）。"""
    T = len(emission)
    log_alpha = [None] * T
    log_alpha[0] = emission[0].astype(float).copy()
    for t in range(1, T):
        trans = transition[t - 1]
        m = np.max(log_alpha[t - 1])
        if not np.isfinite(m):
            m = 0.0
        # logsumexp over prev states
        weighted = log_alpha[t - 1][:, None] + trans
        wmax = np.max(weighted, axis=0)
        wmax_safe = np.where(np.isfinite(wmax), wmax, 0.0)
        summed = np.sum(np.exp(weighted - wmax_safe[None, :]), axis=0)
        log_alpha[t] = np.where(summed > 0, np.log(np.where(summed > 0, summed, 1.0)) + wmax_safe, NEG_INF)
        log_alpha[t] = log_alpha[t] + emission[t]

    log_beta = [None] * T
    log_beta[T - 1] = np.zeros(len(emission[T - 1]))
    for t in range(T - 2, -1, -1):
        trans = transition[t]
        nxt = trans + (emission[t + 1] + log_beta[t + 1])[None, :]
        m = np.max(nxt, axis=1)
        m_safe = np.where(np.isfinite(m), m, 0.0)
        summed = np.sum(np.exp(nxt - m_safe[:, None]), axis=1)
        log_beta[t] = np.where(summed > 0, np.log(np.where(summed > 0, summed, 1.0)) + m_safe, NEG_INF)

    posterior: list[np.ndarray] = []
    for t in range(T):
        log_p = log_alpha[t] + log_beta[t]
        m = np.max(log_p)
        if not np.isfinite(m):
            posterior.append(np.full(len(log_p), 1.0 / len(log_p)))
            continue
        p = np.exp(log_p - m)
        s = np.sum(p)
        posterior.append(p / s if s > 0 else np.full(len(log_p), 1.0 / len(log_p)))
    return posterior
