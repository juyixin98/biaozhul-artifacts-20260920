"""网格地图与最短路距离表（NumPy + SciPy）。"""

from __future__ import annotations

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

# 4 邻域移动（不含等待；等待在规划层单独处理）
MOVES = [(-1, 0), (1, 0), (0, -1), (0, 1)]

Cell = tuple[int, int]


def parse_grid(rows: list[str]) -> np.ndarray:
    """把字符串行（'.' 可通行，'#' 障碍）解析为 0/1 数组，1 表示障碍。"""
    if not rows:
        raise ValueError("grid 不能为空")
    width = len(rows[0])
    if width == 0:
        raise ValueError("grid 行不能为空")
    grid = np.zeros((len(rows), width), dtype=np.int8)
    for r, row in enumerate(rows):
        if len(row) != width:
            raise ValueError("grid 各行长度必须一致")
        for c, ch in enumerate(row):
            if ch == "#":
                grid[r, c] = 1
            elif ch != ".":
                raise ValueError(f"非法网格字符: {ch!r}（仅支持 '.' 与 '#'）")
    return grid


def in_bounds(grid: np.ndarray, cell: Cell) -> bool:
    r, c = cell
    return 0 <= r < grid.shape[0] and 0 <= c < grid.shape[1]


def is_free(grid: np.ndarray, cell: Cell) -> bool:
    return in_bounds(grid, cell) and grid[cell] == 0


def neighbors(grid: np.ndarray, cell: Cell) -> list[Cell]:
    """可通行的 4 邻域格子。"""
    r, c = cell
    out = []
    for dr, dc in MOVES:
        nxt = (r + dr, c + dc)
        if is_free(grid, nxt):
            out.append(nxt)
    return out


def distance_map(grid: np.ndarray, goal: Cell) -> np.ndarray:
    """从 goal 到所有格子的最短路距离（4 邻域、无权），不可达为 inf。

    用 scipy.sparse.csgraph.dijkstra 计算，作为低层 A* 的启发函数。
    """
    h, w = grid.shape
    n = h * w
    rows, cols, data = [], [], []
    for r in range(h):
        for c in range(w):
            if grid[r, c]:
                continue
            u = r * w + c
            for dr, dc in MOVES:
                nr, nc = r + dr, c + dc
                if 0 <= nr < h and 0 <= nc < w and grid[nr, nc] == 0:
                    rows.append(u)
                    cols.append(nr * w + nc)
                    data.append(1.0)
    graph = csr_matrix((data, (rows, cols)), shape=(n, n))
    dist = dijkstra(graph, directed=True, indices=goal[0] * w + goal[1])
    return dist.reshape(h, w)
