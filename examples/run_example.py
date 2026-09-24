"""离线回放示例：对合成场景生成带噪轨迹并运行匹配，打印结果摘要。

用法：python examples/run_example.py
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mapmatching.matcher import MatchConfig, match_trajectory
from mapmatching.scenarios import (
    build_overpass,
    build_parallel_roads,
    noisy_along_line,
)


def summarize(name: str, res) -> None:
    matched = [p for p in res.points if p.matched]
    edges = sorted({p.edge_id for p in matched})
    conf = np.mean([p.confidence for p in matched]) if matched else 0.0
    margin = np.mean([p.margin for p in matched]) if matched else 0.0
    print(f"== {name} ==")
    print(f"  观测点数: {len(res.points)}  匹配: {len(matched)}  未匹配: {len(res.points) - len(matched)}")
    print(f"  匹配到的边: {edges}")
    print(f"  平均置信度: {conf:.3f}  平均置信差: {margin:.3f}")
    print(f"  未匹配段(下标区间): {res.unmatched_segments}")
    print(f"  时间断档位置: {res.segment_breaks}")
    print()


def main() -> None:
    rng = np.random.default_rng(7)

    # 场景 1：平行道路，轨迹沿下方道路
    graph = build_parallel_roads()
    obs = noisy_along_line((50, 0), (950, 0), n=40, dt=2.0, sigma=8.0, rng=rng)
    res = match_trajectory(graph, obs, MatchConfig(candidate_radius=80.0, sigma=8.0))
    summarize("平行道路（轨迹在下方路）", res)

    # 场景 2：立交无连接，轨迹沿水平路穿过交叉点
    graph = build_overpass()
    obs = noisy_along_line(
        (-400, 0), (400, 0), n=50, dt=2.0, sigma=8.0, rng=rng, noise_axes=(True, False)
    )
    res = match_trajectory(graph, obs, MatchConfig(candidate_radius=50.0, sigma=8.0))
    summarize("立交无连接（轨迹在水平路）", res)

    # 场景 3：时间断档 + 离路未匹配段
    graph = build_parallel_roads()
    before = noisy_along_line((50, 0), (350, 0), n=15, dt=2.0, sigma=5.0, rng=rng)
    middle = noisy_along_line((400, 500), (600, 500), n=10, dt=2.0, sigma=5.0, rng=rng,
                              t0=before[-1].t + 2.0)
    after = noisy_along_line((650, 0), (950, 0), n=15, dt=2.0, sigma=5.0, rng=rng,
                             t0=middle[-1].t + 150.0)  # 150s 断档
    res = match_trajectory(graph, before + middle + after,
                           MatchConfig(candidate_radius=50.0, sigma=5.0, max_gap=60.0))
    summarize("时间断档 + 离路段", res)


if __name__ == "__main__":
    main()
