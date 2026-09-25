"""验收测试 3：越界射线处理。

- 终点落在栅格外：栅格内部分照常更新，越界部分安全跳过，不报错；
- 未命中（range >= range_max 或 NaN/inf）：整条射线按空闲处理；
- 越界射线不得把障碍后方误标为空闲之外，也不得产生任何栅格外的写入。
"""

import math

import numpy as np
import pytest

from occupancy_grid import OccupancyGrid, Pose2D, integrate_scan

L_FREE = -0.4


def make_grid() -> OccupancyGrid:
    return OccupancyGrid(width=10, height=10, resolution=1.0)


def test_ray_endpoint_out_of_bounds():
    """未命中射线超出栅格右边界：栅格内单元标空闲，越界终点被忽略。"""
    grid = make_grid()
    stats = integrate_scan(
        grid,
        pose=Pose2D(x=0.5, y=0.5, theta=0.0),
        ranges=[20.0],          # 超过 range_max -> 未命中
        angle_min=0.0,
        angle_increment=0.0,
        range_max=12.0,         # 终点世界 x = 12.5 -> 单元 (12,0)，越界
    )
    assert stats == {"hit": 0, "miss": 1}
    # 栅格内 (0,0)...(9,0) 全部标为空闲
    for col in range(10):
        assert grid.log_odds[0, col] == pytest.approx(L_FREE)
    # 其余行不受影响
    assert np.all(grid.log_odds[1:, :] == 0.0)


def test_ray_starting_outside_grid():
    """传感器在栅格外：只有进入栅格的射线段被融合，不报错。"""
    grid = make_grid()
    stats = integrate_scan(
        grid,
        pose=Pose2D(x=-2.5, y=0.5, theta=0.0),  # 起点单元 (-3, 0)，越界
        ranges=[4.0],                            # 终点 (1.5, 0.5) -> (1,0)，命中
        angle_min=0.0,
        angle_increment=0.0,
        range_max=8.0,
    )
    assert stats == {"hit": 1, "miss": 0}
    # Bresenham (-3,0)->(1,0)：穿越 (-3..0,0)，终点 (1,0)
    assert grid.log_odds[0, 0] == pytest.approx(L_FREE)
    assert grid.log_odds[0, 1] == pytest.approx(0.85)
    assert np.all(grid.log_odds[0, 2:] == 0.0)


def test_diagonal_ray_clipped_at_boundary():
    """斜向射出栅格角落：仅边界内单元更新。"""
    grid = make_grid()
    integrate_scan(
        grid,
        pose=Pose2D(x=8.5, y=8.5, theta=math.pi / 4),  # 指向右上角
        ranges=[10.0],                                  # 未命中，终点远在栅格外
        angle_min=0.0,
        angle_increment=0.0,
        range_max=10.0,
    )
    # 只有 (8,8) 与 (9,9) 在栅格内
    assert grid.log_odds[8, 8] == pytest.approx(L_FREE)
    assert grid.log_odds[9, 9] == pytest.approx(L_FREE)
    assert int(np.count_nonzero(grid.log_odds)) == 2


def test_non_finite_ranges_treated_as_miss():
    """NaN / inf 测距按未命中处理，不抛异常。"""
    grid = make_grid()
    stats = integrate_scan(
        grid,
        pose=Pose2D(x=0.5, y=0.5, theta=0.0),
        ranges=[float("nan"), float("inf")],
        angle_min=0.0,
        angle_increment=math.pi / 2,
        range_max=5.0,
    )
    assert stats == {"hit": 0, "miss": 2}
    assert np.all(grid.log_odds <= 0.0)  # 只有空闲更新


def test_grid_contents_unchanged_outside_bounds():
    """越界射线不得改变栅格形状或产生 NaN。"""
    grid = make_grid()
    integrate_scan(
        grid, Pose2D(0.5, 0.5, 0.0), [100.0], 0.0, 0.0, 50.0
    )
    assert grid.log_odds.shape == (10, 10)
    assert np.all(np.isfinite(grid.log_odds))
