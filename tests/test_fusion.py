"""核心验收测试:单射线手算、重复观测、越界射线、障碍后方保护。"""

import math

import numpy as np
import pytest

from occupancy_grid import (
    GridSpec,
    OccupancyGrid,
    SensorModel,
    SensorPose,
    integrate_scan,
)
from occupancy_grid.fusion import integrate_ray
from occupancy_grid.grid import prob_to_log_odds

L_OCC = prob_to_log_odds(0.7)  # ≈ 0.8473
L_FREE = prob_to_log_odds(0.4)  # ≈ -0.4055


def make_grid(width=10, height=10, resolution=1.0):
    return OccupancyGrid(GridSpec(width, height, resolution))


class TestSingleRayHandCalc:
    """手算验证:传感器在 (0.5, 0.5),朝 +x 发射一条 range=3.0 的射线。

    射线从单元 (0,0) 中心出发,终点 (3.5, 0.5) 落在单元 (0,3)。
    预期:(0,0),(0,1),(0,2) 为空闲;(0,3) 为占据;其余全部未知。
    """

    def test_log_odds_values(self):
        grid = make_grid()
        pose = SensorPose(x=0.5, y=0.5, theta=0.0)
        integrate_ray(grid, pose, SensorModel(), angle=0.0, distance=3.0,
                      max_range=8.0)

        for col in (0, 1, 2):
            assert grid.log_odds[0, col] == pytest.approx(L_FREE)
        assert grid.log_odds[0, 3] == pytest.approx(L_OCC)

        expected = np.zeros((10, 10))
        expected[0, 0:3] = L_FREE
        expected[0, 3] = L_OCC
        np.testing.assert_allclose(grid.log_odds, expected, atol=1e-12)

    def test_probability_values(self):
        grid = make_grid()
        pose = SensorPose(x=0.5, y=0.5, theta=0.0)
        integrate_ray(grid, pose, SensorModel(), angle=0.0, distance=3.0,
                      max_range=8.0)
        prob = grid.to_probability()
        assert prob[0, 3] == pytest.approx(0.7)
        assert prob[0, 0] == pytest.approx(0.4)
        assert prob[5, 5] == pytest.approx(0.5)  # 未观测区域保持未知

    def test_rotated_pose(self):
        """位姿 theta=pi/2 时,angle=0 的射线应指向世界系 +y。"""
        grid = make_grid()
        pose = SensorPose(x=0.5, y=0.5, theta=math.pi / 2)
        integrate_ray(grid, pose, SensorModel(), angle=0.0, distance=3.0,
                      max_range=8.0)
        # 终点 (0.5, 3.5) -> 单元 (3, 0) 占据,纵向穿越单元空闲
        assert grid.log_odds[3, 0] == pytest.approx(L_OCC)
        for row in (0, 1, 2):
            assert grid.log_odds[row, 0] == pytest.approx(L_FREE)


class TestRepeatedObservations:
    """重复观测:log-odds 线性累加,并在上下限处截断。"""

    def test_accumulation(self):
        grid = make_grid()
        pose = SensorPose(x=0.5, y=0.5, theta=0.0)
        for _ in range(3):
            integrate_ray(grid, pose, SensorModel(), angle=0.0, distance=3.0,
                          max_range=8.0)
        assert grid.log_odds[0, 3] == pytest.approx(3 * L_OCC)
        assert grid.log_odds[0, 0] == pytest.approx(3 * L_FREE)

    def test_clamping(self):
        grid = make_grid()  # 默认 l_max = log(0.97/0.03) ≈ 3.476
        pose = SensorPose(x=0.5, y=0.5, theta=0.0)
        for _ in range(100):
            integrate_ray(grid, pose, SensorModel(), angle=0.0, distance=3.0,
                          max_range=8.0)
        assert grid.log_odds[0, 3] == pytest.approx(grid.l_max)
        assert grid.log_odds[0, 0] == pytest.approx(grid.l_min)
        assert np.all(grid.log_odds <= grid.l_max)
        assert np.all(grid.log_odds >= grid.l_min)


class TestObstacleShadow:
    """障碍后方的单元不得被误标为空闲。"""

    def test_cells_behind_hit_stay_unknown(self):
        grid = make_grid()
        pose = SensorPose(x=0.5, y=0.5, theta=0.0)
        integrate_ray(grid, pose, SensorModel(), angle=0.0, distance=3.0,
                      max_range=8.0)
        # 终点 (0,3) 后方的所有单元必须保持 0(未知)
        for col in range(4, 10):
            assert grid.log_odds[0, col] == 0.0

    def test_max_range_miss_marks_free_but_not_occupied(self):
        """未命中射线:穿越单元标空闲,不产生占据单元。"""
        grid = make_grid()
        pose = SensorPose(x=0.5, y=0.5, theta=0.0)
        stats = integrate_scan(grid, pose, SensorModel(),
                               angles=[0.0], ranges=[8.0], max_range=8.0)
        assert stats.miss_count == 1
        assert stats.hit_count == 0
        assert len(stats.occupied_cells) == 0
        # 终点 (8.5, 0.5) 在单元 (0,8):穿越单元 (0,0)..(0,8) 空闲,
        # 射线未到达的 (0,9) 保持未知
        for col in range(0, 9):
            assert grid.log_odds[0, col] == pytest.approx(L_FREE)
        assert grid.log_odds[0, 9] == 0.0


class TestOutOfBoundsRay:
    """越界射线:界内部分正常更新,界外部分安全丢弃,不抛异常。"""

    def test_hit_endpoint_outside_grid(self):
        grid = make_grid()
        pose = SensorPose(x=8.5, y=0.5, theta=0.0)
        stats = integrate_scan(grid, pose, SensorModel(),
                               angles=[0.0], ranges=[20.0], max_range=30.0)
        assert stats.hit_count == 1
        assert stats.out_of_bounds_endpoints == 1
        # 界内穿越单元标空闲
        assert grid.log_odds[0, 8] == pytest.approx(L_FREE)
        assert grid.log_odds[0, 9] == pytest.approx(L_FREE)
        # 没有任何单元被标为占据(终点在界外被丢弃)
        assert np.all(grid.log_odds <= 0.0)

    def test_miss_ray_exiting_grid(self):
        grid = make_grid()
        pose = SensorPose(x=8.5, y=0.5, theta=0.0)
        stats = integrate_scan(grid, pose, SensorModel(),
                               angles=[0.0], ranges=[30.0], max_range=30.0)
        assert stats.miss_count == 1
        assert grid.log_odds[0, 9] == pytest.approx(L_FREE)

    def test_diagonal_ray_exiting_corner(self):
        grid = make_grid()
        pose = SensorPose(x=0.5, y=0.5, theta=0.0)
        stats = integrate_scan(grid, pose, SensorModel(),
                               angles=[math.pi / 4], ranges=[50.0],
                               max_range=60.0)
        assert stats.out_of_bounds_endpoints == 1
        # 对角线穿越的界内单元为空闲,无占据
        assert np.all(grid.log_odds <= 0.0)
        assert grid.log_odds[0, 0] == pytest.approx(L_FREE)
        assert grid.log_odds[9, 9] == pytest.approx(L_FREE)

    def test_sensor_outside_grid_raises(self):
        grid = make_grid()
        pose = SensorPose(x=-5.0, y=0.5, theta=0.0)
        with pytest.raises(ValueError, match="栅格外"):
            integrate_ray(grid, pose, SensorModel(), angle=0.0, distance=1.0,
                          max_range=8.0)


class TestScanIntegration:
    def test_mismatched_angles_ranges_raises(self):
        grid = make_grid()
        with pytest.raises(ValueError, match="不一致"):
            integrate_scan(grid, SensorPose(0.5, 0.5), SensorModel(),
                           angles=[0.0, 1.0], ranges=[1.0], max_range=8.0)

    def test_full_circle_scan_stats(self):
        grid = make_grid()
        pose = SensorPose(x=5.0, y=5.0, theta=0.0)
        angles = [i * math.pi / 4 for i in range(8)]
        ranges = [2.0] * 8
        stats = integrate_scan(grid, pose, SensorModel(), angles, ranges,
                               max_range=8.0)
        assert stats.ray_count == 8
        assert stats.hit_count == 8
        assert len(stats.occupied_cells) == 8
