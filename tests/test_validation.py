"""参数校验与工具函数的边界测试。"""

import pytest

from occupancy_grid import GridSpec, OccupancyGrid, SensorModel, SensorPose
from occupancy_grid.fusion import integrate_ray
from occupancy_grid.geometry import transform_point
from occupancy_grid.grid import prob_to_log_odds


def test_grid_spec_rejects_non_positive_dimensions():
    with pytest.raises(ValueError):
        GridSpec(width=0, height=10, resolution=1.0)
    with pytest.raises(ValueError):
        GridSpec(width=10, height=10, resolution=-0.5)


def test_prob_to_log_odds_rejects_bounds():
    with pytest.raises(ValueError):
        prob_to_log_odds(0.0)
    with pytest.raises(ValueError):
        prob_to_log_odds(1.0)


def test_sensor_model_rejects_inverted_probabilities():
    with pytest.raises(ValueError):
        SensorModel(p_occ=0.4)
    with pytest.raises(ValueError):
        SensorModel(p_free=0.6)


def test_grid_rejects_inverted_clamps():
    with pytest.raises(ValueError):
        OccupancyGrid(GridSpec(5, 5, 1.0), p_min=0.9, p_max=0.1)


def test_ray_rejects_negative_distance():
    grid = OccupancyGrid(GridSpec(10, 10, 1.0))
    with pytest.raises(ValueError):
        integrate_ray(grid, SensorPose(0.5, 0.5), SensorModel(),
                      angle=0.0, distance=-1.0, max_range=8.0)


def test_transform_point_rotation_and_translation():
    import math

    x, y = transform_point(1.0, 0.0, 2.0, 3.0, math.pi / 2)
    assert x == pytest.approx(2.0)
    assert y == pytest.approx(4.0)


def test_cell_center_and_contains():
    grid = OccupancyGrid(GridSpec(10, 10, 0.5, origin_x=1.0, origin_y=2.0))
    cx, cy = grid.cell_center(0, 0)
    assert cx == pytest.approx(1.25)
    assert cy == pytest.approx(2.25)
    assert grid.contains(9, 9)
    assert not grid.contains(10, 0)
    assert not grid.contains(-1, 0)
    assert grid.world_to_cell(1.25, 2.25) == (0, 0)
    assert grid.world_to_cell(-1.0, 0.0) is None


def test_reset_clears_log_odds():
    grid = OccupancyGrid(GridSpec(10, 10, 1.0))
    integrate_ray(grid, SensorPose(0.5, 0.5), SensorModel(),
                  angle=0.0, distance=3.0, max_range=8.0)
    assert grid.log_odds.any()
    grid.reset()
    assert not grid.log_odds.any()
