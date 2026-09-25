"""扫描融合：把一帧二维激光扫描（带传感器位姿）积分进栅格。"""

from __future__ import annotations

import math
from collections.abc import Sequence

from .geometry import Pose2D, sensor_to_world
from .grid import OccupancyGrid


def integrate_scan(
    grid: OccupancyGrid,
    pose: Pose2D,
    ranges: Sequence[float],
    angle_min: float,
    angle_increment: float,
    range_max: float,
) -> dict[str, int]:
    """把一帧激光扫描融合进栅格。

    参数：
        pose: 传感器在世界系中的位姿。
        ranges: 各光束测距（米）。range >= range_max 或非有限值视为未命中，
                射线沿该方向推进到 range_max 处，全部按空闲处理。
        angle_min / angle_increment: 光束角度（传感器系，弧度）。
        range_max: 传感器最大量程。

    返回统计信息 {"hit": n_hit, "miss": n_miss}。
    """
    if angle_increment == 0.0 and len(ranges) > 1:
        raise ValueError("多光束扫描的 angle_increment 不能为 0")
    if range_max <= 0.0:
        raise ValueError("range_max 必须为正")

    start_col, start_row = grid.world_to_grid(pose.x, pose.y)
    n_hit = 0
    n_miss = 0

    for i, r in enumerate(ranges):
        angle = angle_min + i * angle_increment
        hit = math.isfinite(r) and 0.0 < r < range_max
        dist = r if hit else range_max
        # 传感器系 -> 世界系
        end_wx, end_wy = sensor_to_world(
            pose, dist * math.cos(angle), dist * math.sin(angle)
        )
        end_col, end_row = grid.world_to_grid(end_wx, end_wy)
        grid.update_ray(start_col, start_row, end_col, end_row, hit=hit)
        if hit:
            n_hit += 1
        else:
            n_miss += 1

    return {"hit": n_hit, "miss": n_miss}
