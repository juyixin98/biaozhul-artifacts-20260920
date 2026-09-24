"""验收测试：平行道路、噪声点、立交无连接、时间断档、未匹配段。"""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from mapmatching.main import app
from mapmatching.matcher import MatchConfig, match_trajectory
from mapmatching.scenarios import (
    build_overpass,
    build_parallel_roads,
    noisy_along_line,
)

RNG = np.random.default_rng(42)


# ---------- 平行道路 ----------

def test_parallel_roads_stay_on_correct_road():
    """轨迹沿下方道路行驶，候选半径覆盖两条平行路，匹配不应跳到上方道路。"""
    graph = build_parallel_roads()
    obs = noisy_along_line((50, 0), (950, 0), n=40, dt=2.0, sigma=5.0, rng=RNG)
    cfg = MatchConfig(candidate_radius=80.0, sigma=5.0, beta=30.0)
    res = match_trajectory(graph, obs, cfg)

    matched = [p for p in res.points if p.matched]
    assert len(matched) == len(res.points), f"存在未匹配点: {res.unmatched_segments}"
    assert all(p.edge_id == "bottom_E" for p in matched)
    assert res.unmatched_segments == []
    # 置信差字段合法
    for p in matched:
        assert 0.0 <= p.confidence <= 1.0
        assert 0.0 <= p.margin <= 1.0


def test_parallel_roads_no_flip_with_moderate_noise():
    """噪声加大到 10m 时仍应稳定匹配下方道路（HMM 转移约束生效）。

    观测不含航向信息，同一条路的正反向边（bottom_E/bottom_W）几何重合，
    个别点匹配到反向边属已知限制；但不得跳到平行的上方道路。
    """
    graph = build_parallel_roads()
    obs = noisy_along_line((50, 0), (950, 0), n=40, dt=2.0, sigma=10.0, rng=RNG)
    cfg = MatchConfig(candidate_radius=80.0, sigma=10.0, beta=30.0)
    res = match_trajectory(graph, obs, cfg)
    edges = {p.edge_id for p in res.points if p.matched}
    assert edges <= {"bottom_E", "bottom_W"}
    assert not (edges & {"top_E", "top_W"})


# ---------- 立交无连接 ----------

def test_overpass_never_jumps_to_unconnected_edge():
    """轨迹沿水平路穿过立交交叉点，匹配不得跳到几何相交但拓扑不连通的竖直路。"""
    graph = build_overpass()
    # 交叉点 (0,0) 处水平路与竖直路几何距离为 0，竖直路必然成为候选
    obs = noisy_along_line(
        (-400, 0), (400, 0), n=50, dt=2.0, sigma=8.0, rng=RNG,
        noise_axes=(True, False),  # 仅垂直于行进方向加噪，交叉附近点更靠近竖直路
    )
    cfg = MatchConfig(candidate_radius=50.0, sigma=8.0, beta=30.0)
    res = match_trajectory(graph, obs, cfg)

    matched = [p for p in res.points if p.matched]
    assert matched, "应有点被匹配"
    forbidden = {"v_N", "v_S"}
    used = {p.edge_id for p in matched}
    assert not (used & forbidden), f"匹配跳到了未连通边: {used & forbidden}"
    assert used <= {"h_E"}


def test_overpass_graph_topology():
    """立交图中水平节点与竖直节点互不可达。"""
    graph = build_overpass()
    assert not graph.reachable(0, 2)
    assert not graph.reachable(3, 1)
    assert graph.reachable(0, 1)
    assert graph.reachable(2, 3)


# ---------- 时间断档 ----------

def test_time_gap_splits_segments():
    """中间插入 120s 断档，应报告断档位置且两段各自正确匹配。"""
    graph = build_parallel_roads()
    obs = noisy_along_line(
        (50, 0), (950, 0), n=40, dt=2.0, sigma=5.0, rng=RNG,
        gap_after=19, gap_seconds=120.0,
    )
    cfg = MatchConfig(candidate_radius=80.0, sigma=5.0, max_gap=60.0)
    res = match_trajectory(graph, obs, cfg)

    assert res.segment_breaks == [20]
    matched = [p for p in res.points if p.matched]
    assert all(p.edge_id == "bottom_E" for p in matched)


# ---------- 未匹配段 ----------

def test_unmatched_segment_when_off_road():
    """轨迹中段远离所有道路（超出候选半径），应输出未匹配段且前后仍正确匹配。"""
    graph = build_parallel_roads()
    before = noisy_along_line((50, 0), (350, 0), n=15, dt=2.0, sigma=5.0, rng=RNG)
    # 中段：y=500，距任何边都超过候选半径
    middle = noisy_along_line(
        (400, 500), (600, 500), n=10, dt=2.0, sigma=5.0, rng=RNG,
        t0=before[-1].t + 2.0,
    )
    after = noisy_along_line(
        (650, 0), (950, 0), n=15, dt=2.0, sigma=5.0, rng=RNG,
        t0=middle[-1].t + 2.0,
    )
    obs = before + middle + after
    cfg = MatchConfig(candidate_radius=50.0, sigma=5.0, max_gap=300.0)
    res = match_trajectory(graph, obs, cfg)

    assert res.unmatched_segments, "应存在未匹配段"
    # 未匹配段应覆盖中段（下标 15..24）
    assert any(s <= 15 and e >= 24 for s, e in res.unmatched_segments)
    for p in res.points[15:25]:
        assert not p.matched
        assert p.reason == "no_candidate"
    # 前后段仍匹配在下方道路
    for p in res.points[:15] + res.points[25:]:
        assert p.matched and p.edge_id == "bottom_E"


# ---------- API 集成 ----------

def test_api_match_endpoint():
    client = TestClient(app)
    obs = noisy_along_line((50, 0), (950, 0), n=20, dt=2.0, sigma=5.0, rng=RNG)
    resp = client.post(
        "/match",
        json={
            "scenario": "parallel",
            "observations": [{"t": o.t, "x": o.x, "y": o.y} for o in obs],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["points"]) == 20
    assert all(p["edge_id"] == "bottom_E" for p in body["points"] if p["matched"])
    assert "unmatched_segments" in body and "segment_breaks" in body


def test_api_unknown_scenario():
    client = TestClient(app)
    resp = client.post(
        "/match",
        json={"scenario": "nope", "observations": [{"t": 0, "x": 0, "y": 0}]},
    )
    assert resp.status_code == 404
