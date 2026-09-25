"""验收测试 2：重复观测的累积与概率截断。"""

import math

import numpy as np
import pytest

from occupancy_grid import GridParams, OccupancyGrid, Pose2D, integrate_scan

L_OCC = 0.85
L_FREE = -0.4
CLAMP_MIN = -4.0
CLAMP_MAX = 4.0


def make_grid() -> OccupancyGrid:
    return OccupancyGrid(
        width=10,
        height=10,
        resolution=1.0,
        params=GridParams(
            log_odds_occ=L_OCC,
            log_odds_free=L_FREE,
            clamp_min=CLAMP_MIN,
            clamp_max=CLAMP_MAX,
        ),
    )


def fire(grid: OccupancyGrid, n: int) -> None:
    for _ in range(n):
        integrate_scan(
            grid,
            pose=Pose2D(x=0.5, y=0.5, theta=0.0),
            ranges=[3.0],
            angle_min=0.0,
            angle_increment=0.0,
            range_max=8.0,
        )


def test_repeated_observations_accumulate_linearly():
    """n 次相同观测：log-odds 线性累加（未触及截断时）。"""
    grid = make_grid()
    fire(grid, 3)

    for col in (0, 1, 2):
        assert grid.log_odds[0, col] == pytest.approx(3 * L_FREE)
    assert grid.log_odds[0, 3] == pytest.approx(3 * L_OCC)


def test_occupancy_probability_grows_with_evidence():
    """占据概率随重复命中单调上升（未截断区间：4 x 0.85 = 3.4 < 4.0）。"""
    grid = make_grid()
    probs = []
    for _ in range(4):
        fire(grid, 1)
        probs.append(grid.probability_at(3, 0))
    assert all(b > a for a, b in zip(probs, probs[1:]))


def test_occupancy_probability_saturates_at_clamp():
    """继续命中后概率饱和在截断上限对应的概率，不再上升。"""
    grid = make_grid()
    fire(grid, 10)
    p_max = 1.0 - 1.0 / (1.0 + math.exp(CLAMP_MAX))
    assert grid.probability_at(3, 0) == pytest.approx(p_max, abs=1e-12)


def test_log_odds_clamping():
    """大量重复观测后 log-odds 被截断在 [clamp_min, clamp_max]。"""
    grid = make_grid()
    fire(grid, 100)

    assert grid.log_odds[0, 3] == CLAMP_MAX          # 占据方向截断
    assert grid.log_odds[0, 0] == CLAMP_MIN          # 空闲方向截断
    assert np.all(grid.log_odds <= CLAMP_MAX)
    assert np.all(grid.log_odds >= CLAMP_MIN)


def test_conflicting_observations_cancel():
    """同一单元先命中后穿越，log-odds 按代数和抵消。"""
    grid = make_grid()
    # 第一次：终点在 (3,0)，命中
    integrate_scan(grid, Pose2D(0.5, 0.5, 0.0), [3.0], 0.0, 0.0, 8.0)
    # 第二次：更远处的障碍，(3,0) 变为穿越单元
    integrate_scan(grid, Pose2D(0.5, 0.5, 0.0), [6.0], 0.0, 0.0, 8.0)

    assert grid.log_odds[0, 3] == pytest.approx(L_OCC + L_FREE)
    assert grid.log_odds[0, 6] == pytest.approx(L_OCC)
