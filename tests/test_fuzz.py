"""随机化属性测试: 任意增量序列后 D* Lite 始终与独立 Dijkstra 一致。

使用固定种子保证可复现。覆盖障碍新增/移除、权重升降(含降地板触发完整重置)、
起点移动、4/8 邻接、不可达与恢复。
"""

import math
import random

import numpy as np
import pytest

from app.planner import DStarLite, GridView, dijkstra


def _assert_match(view, planner, start, goal):
    bench = dijkstra(view, start, goal)
    cost = planner.optimal_cost()
    if math.isinf(bench["cost"]):
        assert math.isinf(cost)
        assert planner.extract_path() is None
        return
    assert cost == pytest.approx(bench["cost"], abs=1e-6)
    path = planner.extract_path()
    assert path is not None
    total = 0.0
    for a, z in zip(path, path[1:]):
        edge = view.edge_cost(a, z)
        assert edge is not None
        total += edge
    assert total == pytest.approx(bench["cost"], abs=1e-6)


def _run_scenario(seed, trials, steps):
    rng = random.Random(seed)
    for trial in range(trials):
        h = rng.randint(3, 12)
        wdt = rng.randint(3, 12)
        conn = rng.choice([4, 8])
        floor_lo = 0.0 if rng.random() < 0.5 else 0.3
        weights = np.array(
            [
                [rng.uniform(floor_lo, 4.0) for _ in range(wdt)]
                for _ in range(h)
            ]
        )
        blocked = np.zeros((h, wdt), dtype=bool)
        for r in range(h):
            for c in range(wdt):
                if rng.random() < 0.2:
                    blocked[r, c] = True
        cells = [(r, c) for r in range(h) for c in range(wdt)]
        free = [x for x in cells if not blocked[x]]
        if len(free) < 2:
            continue
        start, goal = rng.sample(free, 2)
        planner = DStarLite(GridView(weights, blocked, conn), start, goal)
        _assert_match(GridView(weights, blocked, conn), planner, start, goal)

        for step in range(steps):
            if rng.random() < 0.55:
                changed = []
                for _ in range(rng.randint(1, 4)):
                    r, c = rng.randrange(h), rng.randrange(wdt)
                    if (r, c) in (start, goal):
                        continue
                    kind = rng.random()
                    if kind < 0.45:
                        blocked[r, c] = True
                    elif kind < 0.8:
                        blocked[r, c] = False
                    else:
                        # 含降地板可能: 直接给极小或零值
                        weights[r, c] = rng.choice(
                            [
                                rng.uniform(0.0, 0.05),
                                rng.uniform(0.3, 4.0),
                            ]
                        )
                    changed.append((r, c))
                if changed:
                    planner.update_grid(
                        GridView(weights, blocked, conn), changed
                    )
            else:
                start = rng.choice([x for x in cells if not blocked[x]])
                planner.move_start(start)
            _assert_match(
                GridView(weights, blocked, conn), planner, start, goal
            )


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_fuzz_quick(seed):
    # 快速套件: 每种子 25 局 8 步
    _run_scenario(seed, trials=25, steps=8)


def test_fuzz_unreachable_and_recover_cycle():
    """反复封死/打开目标所在行, 成本必须在 inf 与有限值间正确切换。"""
    rng = random.Random(77)
    h, wdt = 8, 8
    weights = np.ones((h, wdt))
    blocked = np.zeros((h, wdt), dtype=bool)
    planner = DStarLite(GridView(weights, blocked, 8), (0, 0), (7, 7))
    wall_row = 4
    for it in range(6):
        if it % 2 == 0:
            blocked[wall_row, :] = True
            changed = [(wall_row, c) for c in range(wdt)]
        else:
            col = rng.randrange(wdt)
            blocked[wall_row, col] = False
            changed = [(wall_row, col)]
        planner.update_grid(GridView(weights, blocked, 8), changed)
        _assert_match(
            GridView(weights, blocked, 8), planner, (0, 0), (7, 7)
        )


def test_fuzz_interleaved_moves_and_edits():
    """起点移动与地图编辑交错, 增量状态不能基于旧地图给出失效路径。"""
    rng = random.Random(555)
    h = wdt = 10
    weights = np.ones((h, wdt))
    blocked = np.zeros((h, wdt), dtype=bool)
    start, goal = (0, 0), (9, 9)
    planner = DStarLite(GridView(weights, blocked, 8), start, goal)
    cells = [(r, c) for r in range(h) for c in range(wdt)]
    for _ in range(30):
        if rng.random() < 0.5:
            r, c = rng.randrange(h), rng.randrange(wdt)
            # 服务语义: 规划端点(当前起点与固定目标)不允许被堵
            if (r, c) == goal or (r, c) == start:
                continue
            blocked[r, c] = not blocked[r, c]
            planner.update_grid(
                GridView(weights, blocked, 8), [(r, c)]
            )
        else:
            fr = [x for x in cells if not blocked[x]]
            start = rng.choice(fr)
            planner.move_start(start)
        _assert_match(
            GridView(weights, blocked, 8), planner, start, goal
        )
