"""验收测试 1：单射线手算对照。

场景：10x10 栅格，分辨率 1.0，原点 (0,0)。
传感器位于 (0.5, 0.5)（即单元 (0,0) 中心），朝向 +x，测距 3.0（命中）。

手算：
- 终点世界坐标 = (0.5 + 3.0, 0.5) = (3.5, 0.5) -> 单元 (3, 0)
- Bresenham (0,0)->(3,0)：穿越 (0,0), (1,0), (2,0)，终点 (3,0)
- 穿越单元 log-odds = -0.4（空闲），终点 log-odds = +0.85（占据）
- 其余单元保持先验 0（p = 0.5，未知）
- 障碍后方单元 (4,0)...(9,0) 不得被标为空闲
"""

import math

import numpy as np
import pytest

from occupancy_grid import GridParams, OccupancyGrid, Pose2D, integrate_scan

L_OCC = 0.85
L_FREE = -0.4


def make_grid() -> OccupancyGrid:
    return OccupancyGrid(
        width=10,
        height=10,
        resolution=1.0,
        params=GridParams(
            log_odds_occ=L_OCC, log_odds_free=L_FREE, clamp_min=-4.0, clamp_max=4.0
        ),
    )


def fire_single_ray(grid: OccupancyGrid) -> None:
    integrate_scan(
        grid,
        pose=Pose2D(x=0.5, y=0.5, theta=0.0),
        ranges=[3.0],
        angle_min=0.0,
        angle_increment=0.0,
        range_max=8.0,
    )


def log_odds_to_prob(l: float) -> float:
    return 1.0 - 1.0 / (1.0 + math.exp(l))


def test_single_ray_log_odds_hand_computed():
    grid = make_grid()
    fire_single_ray(grid)

    expected = np.zeros((10, 10))
    expected[0, 0] = L_FREE
    expected[0, 1] = L_FREE
    expected[0, 2] = L_FREE
    expected[0, 3] = L_OCC
    np.testing.assert_allclose(grid.log_odds, expected, atol=1e-12)


def test_single_ray_probabilities_hand_computed():
    grid = make_grid()
    fire_single_ray(grid)
    prob = grid.probabilities()

    assert prob[0, 0] == pytest.approx(log_odds_to_prob(L_FREE), abs=1e-12)
    assert prob[0, 2] == pytest.approx(log_odds_to_prob(L_FREE), abs=1e-12)
    assert prob[0, 3] == pytest.approx(log_odds_to_prob(L_OCC), abs=1e-12)
    # 未观测单元保持未知
    assert prob[5, 5] == pytest.approx(0.5, abs=1e-12)


def test_cells_behind_obstacle_not_marked_free():
    """障碍后方（终点之外）的单元必须保持先验，不得被误标为空闲。"""
    grid = make_grid()
    fire_single_ray(grid)

    for col in range(4, 10):
        assert grid.log_odds[0, col] == 0.0, f"单元 ({col},0) 被误更新"
        assert grid.probabilities()[0, col] == pytest.approx(0.5)


def test_rotated_pose_transforms_ray():
    """位姿 theta=pi/2 时射线应指向 +y，验证传感器系->世界系转换。"""
    grid = make_grid()
    integrate_scan(
        grid,
        pose=Pose2D(x=0.5, y=0.5, theta=math.pi / 2),
        ranges=[3.0],
        angle_min=0.0,
        angle_increment=0.0,
        range_max=8.0,
    )
    # 终点 (0.5, 3.5) -> 单元 (0,3)；穿越 (0,0),(0,1),(0,2)
    assert grid.log_odds[3, 0] == pytest.approx(L_OCC)
    assert grid.log_odds[0, 0] == pytest.approx(L_FREE)
    assert grid.log_odds[1, 0] == pytest.approx(L_FREE)
    assert grid.log_odds[2, 0] == pytest.approx(L_FREE)
    # x 方向不应有任何更新
    assert np.all(grid.log_odds[:, 1:] == 0.0)


def test_translated_pose_transforms_ray():
    """传感器平移后射线起点随之移动。"""
    grid = make_grid()
    integrate_scan(
        grid,
        pose=Pose2D(x=2.5, y=0.5, theta=0.0),
        ranges=[2.0],
        angle_min=0.0,
        angle_increment=0.0,
        range_max=8.0,
    )
    # 起点单元 (2,0)，终点 (4.5,0.5) -> (4,0)，穿越 (2,0),(3,0)
    assert grid.log_odds[0, 2] == pytest.approx(L_FREE)
    assert grid.log_odds[0, 3] == pytest.approx(L_FREE)
    assert grid.log_odds[0, 4] == pytest.approx(L_OCC)
    assert grid.log_odds[0, 0] == 0.0
    assert grid.log_odds[0, 1] == 0.0
