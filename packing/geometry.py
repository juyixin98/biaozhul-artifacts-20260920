"""矩形几何原语：合法性、区间重叠与矩形碰撞。

数值约定
--------
* 所有坐标/尺寸以 float 表示；输入先经 :data:`EPS = 1e-9` 容差清洗。
* "零或负尺寸" 判定：``value <= EPS`` 视为非法（覆盖 0、负数以及 -0.0 噪声）。
* 区间重叠使用严格不等式加 EPS：两投影区间仅在有 *正长度* 交集时算重叠，
  共边（x1 == x2）不算碰撞，这对应装箱中允许矩形边界贴合。
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

#: 数值容差：小于该值的正量视为零
EPS: float = 1e-9


def is_positive_size(value: float) -> bool:
    """尺寸是否严格为正（容差 :data:`EPS`）。"""
    return float(value) > EPS


def intervals_overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    """两个一维区间 ``[a0,a1]``、``[b0,b1]`` 是否有正长度交集。

    端点重合（如 ``a1 == b0``）不算重叠。
    """
    return (a0 < b1 - EPS) and (b0 < a1 - EPS)


def rects_overlap(
    ax: float, ay: float, aw: float, ah: float,
    bx: float, by: float, bw: float, bh: float,
) -> bool:
    """两个轴对齐矩形（左下角 + 宽高）是否内部相交（共边不算）。"""
    return intervals_overlap(ax, ax + aw, bx, bx + bw) and intervals_overlap(
        ay, ay + ah, by, by + bh
    )


def placements_collide(x: NDArray[np.float64], y: NDArray[np.float64],
                       w: NDArray[np.float64], h: NDArray[np.float64]) -> list[tuple[int, int]]:
    """批量检测布局中所有矩形对的重叠。

    参数
    ----
    x, y:
        每个矩形左下角坐标，长度 n。
    w, h:
        每个矩形宽、高，长度 n。

    返回
    ----
    list[tuple[int, int]]
        发生正面积重叠的矩形下标对 ``(i, j)``（i < j）；空列表表示无碰撞。
        用 NumPy 广播一次性算出全部 n×n 对。
    """
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    w = np.asarray(w, dtype=np.float64)
    h = np.asarray(h, dtype=np.float64)
    if not (x.shape == y.shape == w.shape == h.shape and x.ndim == 1):
        raise ValueError("x, y, w, h 必须是等长的一维数组")

    # 右侧坐标
    rx = x + w
    ty = y + h
    # 广播得到 n×n 布尔矩阵；两端点重合不算重叠
    ox = (x[:, None] < rx[None, :] - EPS) & (x[None, :] < rx[:, None] - EPS)
    oy = (y[:, None] < ty[None, :] - EPS) & (y[None, :] < ty[:, None] - EPS)
    coll = ox & oy
    iu, ju = np.nonzero(np.triu(coll, k=1))
    return list(zip(iu.tolist(), ju.tolist()))


def clean_scalar(value: float) -> float:
    """清洗单个标量尺寸：去掉 |v| < EPS 的浮点噪声，负值保留为负（校验阶段拒绝）。"""
    v = float(value)
    if abs(v) < EPS:
        return 0.0
    return v
