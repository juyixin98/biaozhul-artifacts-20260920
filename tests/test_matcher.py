"""核心匹配算法测试。"""

import math

import numpy as np
import pytest

from mapmatch.graph import RoadGraph, Candidate
from mapmatch.matcher import MapMatcher, MatchParams
from mapmatch import synthetic


def edge_prefix_ok(matched_edges, prefix):
    return all(e is None or e.startswith(prefix) for e in matched_edges)


# ---------------------------------------------------------------- 平行道路
def test_parallel_roads_mostly_correct_road():
    graph = synthetic.make_parallel_roads_graph()
    rng = np.random.default_rng(123)
    ts, xs, ys, true_edges = synthetic.make_parallel_trajectory(rng, sigma=6.0)
    result = MapMatcher(graph, MatchParams()).match(ts, xs, ys)
    matched = [p for p in result.points if p.matched]
    assert len(matched) / len(ts) >= 0.95
    # 应全部匹配到南侧 A 路（噪声 sigma=6，路间距 20m）
    on_a = sum(p.edge_id.startswith("AE") for p in matched)
    assert on_a / len(matched) >= 0.90
    # 置信差存在且在 [0,1]
    for p in matched:
        assert 0.0 <= p.confidence_margin <= 1.0


# ---------------------------------------------------------------- 立交无连接
def test_overpass_does_not_jump_between_unconnected_edges():
    graph = synthetic.make_overpass_graph()
    rng = np.random.default_rng(99)
    ts, xs, ys, true_edges = synthetic.make_overpass_trajectory(rng, sigma=8.0)
    result = MapMatcher(graph, MatchParams()).match(ts, xs, ys)
    matched = [p for p in result.points if p.matched]
    assert len(matched) / len(ts) >= 0.95

    h_edges = {eid for eid in graph.edges if eid.startswith("HE")}
    v_edges = {eid for eid in graph.edges if eid.startswith("VE")}

    # 1) 绝不跳到垂直路
    used = {p.edge_id for p in matched}
    assert used & v_edges == set(), f"匹配跳到了未连通的垂直路: {used & v_edges}"
    assert used <= h_edges

    # 2) 逐对检查：同一连续片段内相邻匹配边在图上沿路可达（Viterbi 不会凭空跳跃）
    for a, b in zip(matched[:-1], matched[1:]):
        if a.index + 1 != b.index or a.segment_id != b.segment_id:
            continue
        ca = graph.candidates(a.proj_x, a.proj_y, 1e-6)
        cb = graph.candidates(b.proj_x, b.proj_y, 1e-6)
        ca = next(c for c in ca if c.edge_id == a.edge_id)
        cb = next(c for c in cb if c.edge_id == b.edge_id)
        assert math.isfinite(graph.route_distance(ca, cb))


def test_overpass_graph_geometric_crossing_but_topologically_disconnected():
    """两个节点坐标相同但不连通：跨路距离必须为 inf。"""
    graph = synthetic.make_overpass_graph()
    # HN5/VN5 都在 (0,0)
    ca = Candidate("HE4_f", 1.0, 0.0, 0.0, 0.0)  # 水平路边，终点恰为立交点
    cb = Candidate("VE4_f", 0.0, 0.0, 0.0, 0.0)  # 垂直路边，起点恰为立交点
    assert math.isinf(graph.route_distance(ca, cb))
    assert math.isinf(graph.route_distance(cb, ca))
    # 同一路内部可达
    cc = Candidate("HE4_f", 0.0, 0.0, -100.0, 0.0)
    assert math.isfinite(graph.route_distance(cc, ca))


# ---------------------------------------------------------------- 无候选/不可达
def test_far_outlier_is_unmatched_and_reported():
    graph = synthetic.make_gap_graph()
    ts, xs, ys = synthetic.make_far_outlier_trajectory()
    result = MapMatcher(graph, MatchParams(candidate_radius=50.0)).match(ts, xs, ys)
    assert result.points[2].matched is False
    assert result.points[2].reason == "no_candidate"
    assert any(iv.start_index == 2 and iv.end_index == 2 for iv in result.unmatched_intervals)
    # 其余点正常匹配
    assert all(result.points[i].matched for i in [0, 1, 3, 4])


# ---------------------------------------------------------------- 时间断档
def test_time_gap_splits_segments():
    graph = synthetic.make_gap_graph()
    ts, xs, ys = synthetic.make_gap_trajectory()
    result = MapMatcher(graph, MatchParams(max_time_gap=10.0)).match(ts, xs, ys)
    assert result.segments == 2
    assert len(result.time_gaps) == 1
    gap = result.time_gaps[0]
    assert gap.index == 10
    assert gap.dt == pytest.approx(60.0)
    # 两段所有点均匹配，但分属不同 segment_id
    seg_ids = {p.segment_id for p in result.points if p.matched}
    assert seg_ids == {0, 1}


def test_no_gap_single_segment():
    graph = synthetic.make_gap_graph()
    ts = [float(i) for i in range(5)]
    xs = [100.0 + 10 * i for i in range(5)]
    ys = [0.0] * 5
    result = MapMatcher(graph).match(ts, xs, ys)
    assert result.segments == 1
    assert result.time_gaps == []
    assert all(p.matched for p in result.points)


# ---------------------------------------------------------------- 后处理结构
def test_confidence_and_low_confidence_flag():
    graph = synthetic.make_parallel_roads_graph()
    rng = np.random.default_rng(5)
    ts, xs, ys, _ = synthetic.make_parallel_trajectory(rng, sigma=9.0)
    result = MapMatcher(
        graph, MatchParams(confidence_threshold=0.5)
    ).match(ts, xs, ys)
    for p in result.points:
        if p.matched:
            assert p.confidence_margin is not None
            assert p.low_confidence == (p.confidence_margin < 0.5)


def test_empty_trajectory_allowed_by_matcher():
    graph = synthetic.make_gap_graph()
    result = MapMatcher(graph).match([], [], [])
    assert result.points == []
    assert result.segments == 0
