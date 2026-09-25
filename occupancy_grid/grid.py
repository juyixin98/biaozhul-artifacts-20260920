"""占据栅格：log-odds 存储、射线更新、概率截断。"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .geometry import bresenham

# 默认更新参数（与常见占据栅格实现一致的经验取值）
DEFAULT_LOG_ODDS_OCC = 0.85   # p_occ  ≈ 0.70
DEFAULT_LOG_ODDS_FREE = -0.4  # p_free ≈ 0.40
DEFAULT_CLAMP_MIN = -4.0      # p ≈ 0.018
DEFAULT_CLAMP_MAX = 4.0       # p ≈ 0.982


@dataclass(frozen=True)
class GridParams:
    """融合参数。"""

    log_odds_occ: float = DEFAULT_LOG_ODDS_OCC
    log_odds_free: float = DEFAULT_LOG_ODDS_FREE
    clamp_min: float = DEFAULT_CLAMP_MIN
    clamp_max: float = DEFAULT_CLAMP_MAX

    def __post_init__(self) -> None:
        if self.log_odds_occ <= 0.0:
            raise ValueError("log_odds_occ 必须为正")
        if self.log_odds_free >= 0.0:
            raise ValueError("log_odds_free 必须为负")
        if self.clamp_min >= self.clamp_max:
            raise ValueError("clamp_min 必须小于 clamp_max")
        if not (self.clamp_min <= 0.0 <= self.clamp_max):
            raise ValueError("截断区间必须包含 0（先验）")


@dataclass
class OccupancyGrid:
    """二维占据栅格，内部以 log-odds 存储。

    log_odds[row, col]：行对应世界 y，列对应世界 x。
    先验为 0（p = 0.5，未知）。
    """

    width: int            # 列数（x 方向）
    height: int           # 行数（y 方向）
    resolution: float     # 单元边长（米）
    origin_x: float = 0.0  # 栅格左下角的世界坐标
    origin_y: float = 0.0
    params: GridParams = field(default_factory=GridParams)

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("栅格尺寸必须为正")
        if self.resolution <= 0.0:
            raise ValueError("分辨率必须为正")
        self.log_odds = np.zeros((self.height, self.width), dtype=np.float64)

    # ---- 坐标换算 ------------------------------------------------------

    def in_bounds(self, col: int, row: int) -> bool:
        return 0 <= col < self.width and 0 <= row < self.height

    def world_to_grid(self, wx: float, wy: float) -> tuple[int, int]:
        import math

        col = math.floor((wx - self.origin_x) / self.resolution)
        row = math.floor((wy - self.origin_y) / self.resolution)
        return (col, row)

    # ---- 核心更新 ------------------------------------------------------

    def update_ray(
        self, start_col: int, start_row: int, end_col: int, end_row: int, hit: bool
    ) -> None:
        """融合单条射线。

        - 穿越单元（不含终点）按空闲更新；
        - 终点单元：hit=True 时按占据更新（仅当终点在栅格内）；
          hit=False（未命中/超量程）时终点也按空闲处理；
        - 完全落在栅格外的部分自动跳过，越界不报错。
        """
        cells = bresenham(start_col, start_row, end_col, end_row)
        if hit:
            free_cells, occ_cells = cells[:-1], cells[-1:]
        else:
            free_cells, occ_cells = cells, []

        p = self.params
        for col, row in free_cells:
            if self.in_bounds(col, row):
                self.log_odds[row, col] += p.log_odds_free
        for col, row in occ_cells:
            if self.in_bounds(col, row):
                self.log_odds[row, col] += p.log_odds_occ

        # 概率截断：限制 log-odds 幅度，防止无限累积
        np.clip(self.log_odds, p.clamp_min, p.clamp_max, out=self.log_odds)

    # ---- 查询 ----------------------------------------------------------

    def probabilities(self) -> np.ndarray:
        """返回占据概率矩阵 p = 1 - 1/(1 + exp(l))，形状同 log_odds。"""
        return 1.0 - 1.0 / (1.0 + np.exp(self.log_odds))

    def probability_at(self, col: int, row: int) -> float:
        if not self.in_bounds(col, row):
            raise IndexError(f"单元 ({col}, {row}) 越界")
        return float(self.probabilities()[row, col])
