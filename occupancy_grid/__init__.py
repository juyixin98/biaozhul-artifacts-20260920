"""离散占据栅格融合库（纯后端，离线计算）。

基于 log-odds 的二维占据栅格更新：
- 射线终点单元按"占据"更新；
- 射线穿越单元按"空闲"更新；
- log-odds 概率截断，避免数值发散；
- 传感器位姿 (x, y, theta) 用于传感器系 -> 世界系坐标转换。
"""

from .geometry import Pose2D, bresenham, sensor_to_world, world_to_grid
from .grid import GridParams, OccupancyGrid
from .fusion import integrate_scan

__all__ = [
    "Pose2D",
    "bresenham",
    "sensor_to_world",
    "world_to_grid",
    "GridParams",
    "OccupancyGrid",
    "integrate_scan",
]
