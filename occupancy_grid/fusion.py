"""激光射线与占据栅格的 log-odds 融合。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from occupancy_grid.geometry import bresenham_cells, transform_point
from occupancy_grid.grid import OccupancyGrid, prob_to_log_odds


@dataclass(frozen=True)
class SensorPose:
    """传感器在世界系下的二维位姿。"""

    x: float
    y: float
    theta: float = 0.0  # 弧度,逆时针为正


@dataclass(frozen=True)
class SensorModel:
    """逆传感器模型参数(以概率给定,内部转 log-odds)。

    p_occ:命中单元为占据的观测概率(> 0.5)。
    p_free:穿越单元为占据的观测概率(< 0.5)。
    """

    p_occ: float = 0.7
    p_free: float = 0.4

    def __post_init__(self) -> None:
        if not 0.5 < self.p_occ < 1.0:
            raise ValueError("p_occ 必须在 (0.5, 1) 内")
        if not 0.0 < self.p_free < 0.5:
            raise ValueError("p_free 必须在 (0, 0.5) 内")

    @property
    def l_occ(self) -> float:
        return prob_to_log_odds(self.p_occ)

    @property
    def l_free(self) -> float:
        return prob_to_log_odds(self.p_free)


@dataclass
class ScanStats:
    """单帧扫描的融合统计,用于测试与诊断。"""

    ray_count: int = 0
    hit_count: int = 0  # 有效命中(range < max_range)的射线数
    miss_count: int = 0  # 未命中(自由穿透到 max_range)的射线数
    out_of_bounds_endpoints: int = 0  # 终点落在栅格外被丢弃的命中数
    free_cells: set[tuple[int, int]] = field(default_factory=set)
    occupied_cells: set[tuple[int, int]] = field(default_factory=set)


def _clip_cells_to_grid(
    grid: OccupancyGrid, cells: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """从起点侧保留栅格内的连续前缀,越界即截断。"""
    kept: list[tuple[int, int]] = []
    for row, col in cells:
        if not grid.contains(row, col):
            break
        kept.append((row, col))
    return kept


def integrate_ray(
    grid: OccupancyGrid,
    pose: SensorPose,
    model: SensorModel,
    angle: float,
    distance: float,
    max_range: float,
    stats: ScanStats | None = None,
) -> None:
    """把单条激光射线融合进栅格。

    - 命中(distance < max_range):穿越单元加 l_free,终点单元加 l_occ。
    - 未命中(distance >= max_range):沿 max_range 方向的穿越单元加
      l_free,不产生占据证据。
    - 终点或路径越界:越界部分直接丢弃,障碍后方的单元不会被触碰。
    """
    if distance < 0.0 or max_range <= 0.0:
        raise ValueError("distance 必须 >= 0 且 max_range 必须 > 0")

    hit = distance < max_range
    ray_length = distance if hit else max_range
    world_angle = pose.theta + angle

    start_cell = grid.world_to_cell(pose.x, pose.y)
    if start_cell is None:
        raise ValueError(
            f"传感器位置 ({pose.x}, {pose.y}) 在栅格外,无法融合"
        )

    end_x = pose.x + math.cos(world_angle) * ray_length
    end_y = pose.y + math.sin(world_angle) * ray_length
    end_cell = grid.world_to_cell(end_x, end_y)

    if end_cell is None:
        # 终点越界:沿射线方向取一个足够远的点,换算成(可能越界的)
        # 原始单元坐标做 Bresenham,再按从起点开始的界内前缀截断,
        # 只更新界内穿越单元,不产生占据证据。
        far = ray_length + (grid.spec.width + grid.spec.height) * grid.spec.resolution
        far_x = pose.x + math.cos(world_angle) * far
        far_y = pose.y + math.sin(world_angle) * far
        far_col = int(
            math.floor((far_x - grid.spec.origin_x) / grid.spec.resolution)
        )
        far_row = int(
            math.floor((far_y - grid.spec.origin_y) / grid.spec.resolution)
        )
        cells = _clip_cells_to_grid(
            grid, bresenham_cells(*start_cell, far_row, far_col)
        )
        free_cells = cells
        occupied_cells: list[tuple[int, int]] = []
        if stats is not None and hit:
            stats.out_of_bounds_endpoints += 1
    else:
        cells = bresenham_cells(*start_cell, *end_cell)
        if hit:
            free_cells = cells[:-1]
            occupied_cells = [cells[-1]]
        else:
            free_cells = cells
            occupied_cells = []

    grid.add_log_odds(free_cells, model.l_free)
    grid.add_log_odds(occupied_cells, model.l_occ)

    if stats is not None:
        stats.ray_count += 1
        if hit:
            stats.hit_count += 1
        else:
            stats.miss_count += 1
        stats.free_cells.update(free_cells)
        stats.occupied_cells.update(occupied_cells)


def integrate_scan(
    grid: OccupancyGrid,
    pose: SensorPose,
    model: SensorModel,
    angles: list[float],
    ranges: list[float],
    max_range: float,
) -> ScanStats:
    """融合一帧扫描(等长 angles/ranges,角度为传感器系弧度)。"""
    if len(angles) != len(ranges):
        raise ValueError(
            f"angles 长度 {len(angles)} 与 ranges 长度 {len(ranges)} 不一致"
        )
    stats = ScanStats()
    for angle, distance in zip(angles, ranges):
        integrate_ray(grid, pose, model, angle, distance, max_range, stats)
    return stats
