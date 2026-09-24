"""离线示例：运行三个合成场景并打印匹配结果摘要。

用法: python examples/run_example.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mapmatch import synthetic
from mapmatch.matcher import MapMatcher, MatchParams


def run_parallel():
    graph = synthetic.make_parallel_roads_graph()
    rng = np.random.default_rng(2024)
    ts, xs, ys, true_edges = synthetic.make_parallel_trajectory(rng, sigma=6.0)
    res = MapMatcher(graph, MatchParams()).match(ts, xs, ys)
    matched = [p for p in res.points if p.matched]
    correct = sum(p.edge_id == true_edges[p.index] for p in matched)
    on_a = sum(p.edge_id.startswith("AE") for p in matched)
    margins = np.array([p.confidence_margin for p in matched])
    print("== 场景1：平行道路（路间距 20m，噪声 sigma=6m）==")
    print(f"  观测点 {len(ts)}，匹配 {len(matched)}，行驶方向上的边正确率 {correct}/{len(matched)}")
    print(f"  落在 A 路的比例 {on_a}/{len(matched)}，未匹配区间 {len(res.unmatched_intervals)}")
    print(f"  置信差: 均值 {margins.mean():.3f}, 最小 {margins.min():.3f}")


def run_overpass():
    graph = synthetic.make_overpass_graph()
    rng = np.random.default_rng(2025)
    ts, xs, ys, _ = synthetic.make_overpass_trajectory(rng, sigma=8.0)
    res = MapMatcher(graph, MatchParams()).match(ts, xs, ys)
    matched = [p for p in res.points if p.matched]
    jumped = [p for p in matched if p.edge_id.startswith("VE")]
    margins = np.array([p.confidence_margin for p in matched])
    print("== 场景2：立交无连接（水平路轨迹穿过几何交点）==")
    print(f"  观测点 {len(ts)}，匹配 {len(matched)}，跳到未连通垂直路的点 {len(jumped)}")
    print(f"  未匹配区间 {len(res.unmatched_intervals)}，置信差均值 {margins.mean():.3f}")
    print("  立交点附近匹配明细：")
    for p in matched:
        if abs(p.x) <= 30:
            print(f"    t={p.t:>4.0f} obs=({p.x:7.2f},{p.y:6.2f}) -> {p.edge_id} "
                  f"frac={p.fraction:.2f} margin={p.confidence_margin:.3f}")


def run_gap():
    graph = synthetic.make_gap_graph()
    ts, xs, ys = synthetic.make_gap_trajectory()
    res = MapMatcher(graph, MatchParams(max_time_gap=10.0)).match(ts, xs, ys)
    print("== 场景3：时间断档（中间 60s 无观测）==")
    print(f"  片段数 {res.segments}，断档 {[(g.index, g.dt) for g in res.time_gaps]}")
    print(f"  未匹配区间 {len(res.unmatched_intervals)}")


def run_outlier():
    graph = synthetic.make_gap_graph()
    ts, xs, ys = synthetic.make_far_outlier_trajectory()
    res = MapMatcher(graph, MatchParams(candidate_radius=50.0)).match(ts, xs, ys)
    print("== 场景4：远离路网的离群点 ==")
    for p in res.points:
        print(f"    idx={p.index} matched={p.matched} edge={p.edge_id} reason={p.reason}")
    print(f"  未匹配区间: {[(i.start_index, i.end_index, i.reason) for i in res.unmatched_intervals]}")


if __name__ == "__main__":
    run_parallel()
    run_overpass()
    run_gap()
    run_outlier()
