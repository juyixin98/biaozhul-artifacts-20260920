"""地面分割参数。

所有阈值都有明确的物理含义，代码里不会做“取最大平面当地面”这种隐式假设：
一个平面只有同时通过倾角门槛、内点比例门槛，才算“可靠地面”。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GroundSegParams:
    # —— RANSAC 平面拟合 ——
    ransac_iterations: int = 200
    """每个块内 RANSAC 最大迭代次数（迭代本身由随机种子确定性驱动）。"""

    distance_threshold: float = 0.15
    """点到拟合平面的距离阈值（与点云同单位），小于该值算平面内点。"""

    seed_rng: int = 20260923
    """随机数种子。同一输入 + 同一参数，输出逐字节确定。"""

    seed_indices: tuple[int, ...] | None = None
    """调用方给定的全局点 ID，作为首个 RANSAC 三点假设的先验种子
    （例如已知地面控制点）。可选；不给则完全由种子 RNG 采样。"""

    # —— 地面可靠性门槛 ——
    max_tilt_deg: float = 20.0
    """平面法向与竖直 +Z 方向的最大允许夹角（度）。
    超过该倾角的平面（陡坡、屋顶等）不会被当作地面。"""

    min_inlier_ratio: float = 0.5
    """块内点云属于平面的最小内点比例，低于该值判“不可靠”。"""

    min_inlier_count: int = 20
    """平面内点绝对数量下限，防止稀疏点云用比例门槛蒙混过关。"""

    min_unique_points: int = 12
    """块内去重后唯一点数下限；低于该值为数据不足，直接不可判定。"""

    # —— 点级分类 ——
    point_margin: float = 1.5
    """点级判定相对 distance_threshold 的放宽倍数。
    内点距离 <= margin*阈值 才参与地面/非地面投票。"""

    # —— 分块 ——
    tile_size: float = 4.0
    """分块（XY 正方形网格）边长。<=0 表示整块点云作为单块处理。"""

    tile_overlap: float = 0.5
    """相邻块的重叠比例，取值 [0,1)。默认 0.5 即半格重叠。"""

    # —— 重叠区冲突合并 ——
    conflict_margin: float = 1.25
    """重叠块投票冲突时，高置信一侧必须严格胜出该倍数才采纳，
    否则该点保守地判为 undecidable。"""

    def validate(self) -> None:
        if self.ransac_iterations < 1:
            raise ValueError("ransac_iterations 必须 >= 1")
        if self.distance_threshold <= 0:
            raise ValueError("distance_threshold 必须 > 0")
        if not (0.0 < self.max_tilt_deg < 90.0):
            raise ValueError("max_tilt_deg 必须在 (0, 90) 之间")
        if not (0.0 < self.min_inlier_ratio <= 1.0):
            raise ValueError("min_inlier_ratio 必须在 (0, 1] 之间")
        if self.min_inlier_count < 3:
            raise ValueError("min_inlier_count 必须 >= 3")
        if self.min_unique_points < 3:
            raise ValueError("min_unique_points 必须 >= 3")
        if self.point_margin < 1.0:
            raise ValueError("point_margin 必须 >= 1")
        if self.tile_overlap < 0.0 or self.tile_overlap >= 1.0:
            raise ValueError("tile_overlap 必须在 [0, 1) 之间")
        if self.conflict_margin <= 1.0:
            raise ValueError("conflict_margin 必须 > 1")
