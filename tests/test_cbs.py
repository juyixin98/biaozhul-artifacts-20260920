"""CBS 与穷举核对的核心测试：交叉口、单通道、让行港湾、无解与随机小实例。"""

import random

import pytest

from mrcbs.brute_force import brute_force_solve
from mrcbs.cbs import pad_timetable, solve_cbs
from mrcbs.grid import parse_grid
from mrcbs.validate import validate_timetable

# 交叉口：3x3 全开，两机器人垂直穿越中心格
INTERSECTION = ["...", "...", "..."]
INTERSECTION_STARTS = [(1, 0), (0, 1)]
INTERSECTION_GOALS = [(1, 2), (2, 1)]

# 单通道：1x3，两机器人对向互换 —— 无解
CORRIDOR = ["..."]
CORRIDOR_STARTS = [(0, 0), (0, 2)]
CORRIDOR_GOALS = [(0, 2), (0, 0)]

# 带让行港湾的通道：中间下方 (1,1) 有一个避让格 —— 有解
BAY_GRID = ["...", "#.#"]
BAY_STARTS = [(0, 0), (0, 2)]
BAY_GOALS = [(0, 2), (0, 0)]

# 目标被永久堵死：A0 目标 (0,1) 位于通道内，A1 必须穿过 —— 无解
BLOCKED = ["...."]
BLOCKED_STARTS = [(0, 0), (0, 2)]
BLOCKED_GOALS = [(0, 1), (0, 0)]


def solve_and_validate(grid_rows, starts, goals, **kw):
    grid = parse_grid(grid_rows)
    result = solve_cbs(grid, starts, goals, **kw)
    if result.paths is not None:
        timetable = pad_timetable(result.paths)
        violations = validate_timetable(grid, starts, goals, timetable)
        assert violations == [], f"解不合法: {violations}"
    return grid, result


class TestIntersection:
    def test_optimal_cost(self):
        _, result = solve_and_validate(INTERSECTION, INTERSECTION_STARTS, INTERSECTION_GOALS)
        assert result.status == "optimal"
        # 双方最短均为 2，中心格冲突须有一方等待 1 步
        assert result.cost == 5

    def test_matches_brute_force(self):
        grid = parse_grid(INTERSECTION)
        bf = brute_force_solve(grid, INTERSECTION_STARTS, INTERSECTION_GOALS)
        _, result = solve_and_validate(INTERSECTION, INTERSECTION_STARTS, INTERSECTION_GOALS)
        assert bf is not None and result.cost == bf[0]

    def test_stats_present(self):
        _, result = solve_and_validate(INTERSECTION, INTERSECTION_STARTS, INTERSECTION_GOALS)
        for key in ("nodes_expanded", "nodes_generated", "conflicts_detected",
                    "constraints_in_solution", "max_open_size"):
            assert key in result.stats
        assert result.stats["conflicts_detected"] >= 1


class TestCorridorUnsolvable:
    def test_cbs_reports_unsolvable(self):
        _, result = solve_and_validate(CORRIDOR, CORRIDOR_STARTS, CORRIDOR_GOALS)
        assert result.status == "unsolvable"

    def test_brute_force_agrees(self):
        grid = parse_grid(CORRIDOR)
        assert brute_force_solve(grid, CORRIDOR_STARTS, CORRIDOR_GOALS) is None


class TestPassingBay:
    def test_solvable_and_optimal(self):
        grid, result = solve_and_validate(BAY_GRID, BAY_STARTS, BAY_GOALS)
        assert result.status == "optimal"
        bf = brute_force_solve(grid, BAY_STARTS, BAY_GOALS)
        assert bf is not None and result.cost == bf[0]


class TestBlockedGoal:
    def test_unsolvable(self):
        _, result = solve_and_validate(BLOCKED, BLOCKED_STARTS, BLOCKED_GOALS)
        assert result.status == "unsolvable"
        grid = parse_grid(BLOCKED)
        assert brute_force_solve(grid, BLOCKED_STARTS, BLOCKED_GOALS) is None


class TestGoalPersistence:
    def test_agent_stays_at_goal(self):
        # A0 目标在通道中间，A1 在 A0 到达后不得与其冲突；实例本身可解
        grid_rows = ["...", "..."]
        starts = [(0, 0), (1, 2)]
        goals = [(0, 2), (1, 0)]
        grid, result = solve_and_validate(grid_rows, starts, goals)
        assert result.status == "optimal"
        timetable = pad_timetable(result.paths)
        for path, goal in zip(timetable, goals):
            arrival = path.index(goal)
            assert all(cell == goal for cell in path[arrival:])


class TestRandomizedAgainstBruteForce:
    @pytest.mark.parametrize("seed", range(20))
    def test_random_small_instances(self, seed):
        rng = random.Random(seed)
        h, w = rng.choice([(3, 3), (3, 4), (4, 4)])
        n_agents = rng.choice([2, 2, 3])
        # 随机障碍（约 1/4），保证至少留出足够自由格
        cells = [(r, c) for r in range(h) for c in range(w)]
        rng.shuffle(cells)
        n_block = len(cells) // 4
        blocked = set(cells[:n_block])
        free = [c for c in cells if c not in blocked]
        if len(free) < n_agents * 2:
            pytest.skip("自由格不足")
        picks = rng.sample(free, n_agents * 2)
        starts, goals = picks[:n_agents], picks[n_agents:]
        rows = [[("#" if (r, c) in blocked else ".") for c in range(w)] for r in range(h)]
        grid_rows = ["".join(row) for row in rows]

        grid = parse_grid(grid_rows)
        result = solve_cbs(grid, starts, goals, max_nodes=20000)
        bf = brute_force_solve(grid, starts, goals)
        if bf is None:
            assert result.status == "unsolvable", (
                f"seed={seed} 穷举无解但 CBS 返回 {result.status}")
        else:
            assert result.status == "optimal", (
                f"seed={seed} 穷举有解但 CBS 返回 {result.status}")
            assert result.cost == bf[0], (
                f"seed={seed} CBS 代价 {result.cost} != 穷举 {bf[0]}")
            timetable = pad_timetable(result.paths)
            assert validate_timetable(grid, starts, goals, timetable) == []
