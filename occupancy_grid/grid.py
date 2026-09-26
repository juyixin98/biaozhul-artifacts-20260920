"""占据栅格数据结构:log-odds 表示、坐标转换、概率截断。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class GridSpec:
    """栅格几何参数。

    origin_x/origin_y 为栅格左下角(单元 (0, 0) 的角点)在世界系下的坐标。
    """

    width: int  # 列数(x 方向单元数)
    height: int  # 行数(y 方向单元数)
    resolution: float  # 单元边长,米/单元
    origin_x: float = 0.0
    origin_y: float = 0.0

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("width/height 必须为正整数")
        if self.resolution <= 0:
            raise ValueError("resolution 必须为正数")


# 默认概率截断边界(对应 log-odds 上下限)
DEFAULT_P_MIN = 0.12
DEFAULT_P_MAX = 0.97


def prob_to_log_odds(p: float) -> float:
    """概率转 log-odds:log(p / (1 - p))。"""
    if not 0.0 < p < 1.0:
        raise ValueError(f"概率必须在 (0, 1) 开区间内,得到 {p}")
    return float(np.log(p / (1.0 - p)))


def log_odds_to_prob(log_odds: np.ndarray) -> np.ndarray:
    """log-odds 转概率:p = 1 - 1 / (1 + exp(l))。"""
    return 1.0 - 1.0 / (1.0 + np.exp(log_odds))


class OccupancyGrid:
    """以 log-odds 存储的二维占据栅格。

    log_odds[row, col]:0 表示未知,正值倾向占据,负值倾向空闲。
    每次更新后按 [l_min, l_max] 截断,防止数值无界增长并允许
    长期不再观测的单元被反向证据缓慢纠正。
    """

    def __init__(
        self,
        spec: GridSpec,
        p_min: float = DEFAULT_P_MIN,
        p_max: float = DEFAULT_P_MAX,
    ) -> None:
        self.spec = spec
        self.l_min = prob_to_log_odds(p_min)
        self.l_max = prob_to_log_odds(p_max)
        if self.l_min >= self.l_max:
            raise ValueError("p_min 必须小于 p_max")
        self.log_odds = np.zeros((spec.height, spec.width), dtype=np.float64)

    def world_to_cell(self, x: float, y: float) -> tuple[int, int] | None:
        """世界坐标转 (row, col);越界返回 None。"""
        col = int(np.floor((x - self.spec.origin_x) / self.spec.resolution))
        row = int(np.floor((y - self.spec.origin_y) / self.spec.resolution))
        if 0 <= row < self.spec.height and 0 <= col < self.spec.width:
            return row, col
        return None

    def cell_center(self, row: int, col: int) -> tuple[float, float]:
        """单元中心的世界坐标。"""
        x = self.spec.origin_x + (col + 0.5) * self.spec.resolution
        y = self.spec.origin_y + (row + 0.5) * self.spec.resolution
        return x, y

    def contains(self, row: int, col: int) -> bool:
        return 0 <= row < self.spec.height and 0 <= col < self.spec.width

    def add_log_odds(self, cells: list[tuple[int, int]], delta: float) -> None:
        """对一组 (row, col) 单元累加 log-odds 并截断。

        同一单元在 cells 中出现多次只累加一次(一条射线对同一单元
        只应产生一次证据)。
        """
        if not cells:
            return
        unique = sorted(set(cells))
        rows = [r for r, _ in unique]
        cols = [c for _, c in unique]
        self.log_odds[rows, cols] += delta
        np.clip(self.log_odds, self.l_min, self.l_max, out=self.log_odds)

    def to_probability(self) -> np.ndarray:
        """导出占据概率栅格(0.5 为未知)。"""
        return log_odds_to_prob(self.log_odds)

    def reset(self) -> None:
        self.log_odds.fill(0.0)
