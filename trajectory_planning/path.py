"""折线几何：长度、单位切向、转角、重复点折叠。

零长段（相邻重合点）在任何除法之前即被识别并跳过，
从根本上避免除零，而不是依赖事后补丁。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

DUPLICATE_TOL = 1e-12


@dataclass(frozen=True)
class Polyline:
    """折叠掉相邻重复点后的折线。

    Attributes:
        points: 形状 (m, d) 的节点坐标，m 为去重后的节点数。
        removed_duplicates: 被折叠掉的相邻重复点数量。
    """

    points: np.ndarray
    removed_duplicates: int

    @property
    def n_nodes(self) -> int:
        return int(self.points.shape[0])

    @property
    def n_segments(self) -> int:
        return self.n_nodes - 1

    @property
    def lengths(self) -> np.ndarray:
        return segment_lengths(self.points)

    @property
    def total_length(self) -> float:
        return float(self.lengths.sum())


def build_polyline(raw_points, tol: float = DUPLICATE_TOL) -> Polyline:
    """由嵌套序列构造折线，折叠相邻重复点。

    Raises:
        ValueError: 不是至少两个点、维度不一致、含非有限数，
            或折叠后不足两个互异点。
    """
    try:
        points = np.asarray(list(raw_points), dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"points 无法转换为数值坐标数组: {exc}") from exc

    if points.ndim != 2 or points.shape[0] < 2:
        raise ValueError("points 必须是至少包含两个点的二维数组 [p0, p1, ...]")
    if points.shape[1] < 1:
        raise ValueError("每个点至少需要 1 个坐标分量")
    if not np.all(np.isfinite(points)):
        raise ValueError("points 中存在 NaN 或 Inf")

    keep = np.ones(points.shape[0], dtype=bool)
    for i in range(1, points.shape[0]):
        if np.linalg.norm(points[i] - points[i - 1]) <= tol:
            keep[i] = False
    cleaned = points[keep]
    removed = int(points.shape[0] - cleaned.shape[0])

    if cleaned.shape[0] < 2:
        raise ValueError("所有点完全重合，路径长度为零，无法进行时间参数化")

    cleaned.setflags(write=False)
    return Polyline(points=cleaned, removed_duplicates=removed)


def segment_lengths(points: np.ndarray) -> np.ndarray:
    """各段长度，形状 (m-1,)；零长段长度为 0（不做除法）。"""
    return np.linalg.norm(np.diff(points, axis=0), axis=1)


def unit_tangents(points: np.ndarray, lengths: np.ndarray) -> np.ndarray:
    """各段单位切向量；零长段对应行返回零向量而非除零。"""
    tangents = np.zeros_like(points, shape=(points.shape[0] - 1, points.shape[1]))
    nonzero = lengths > 0.0
    tangents[nonzero] = np.diff(points, axis=0)[nonzero] / lengths[nonzero, None]
    return tangents


def turning_angles(points: np.ndarray, lengths: np.ndarray) -> np.ndarray:
    """内部节点的方向转角（弧度），形状 (m-2,)。

    0 表示直行，pi 表示完全折返（尖点）。零长段被跳过。
    """
    tangents = unit_tangents(points, lengths)
    angles = np.zeros(max(points.shape[0] - 2, 0))
    for i in range(1, points.shape[0] - 1):
        t_in, t_out = tangents[i - 1], tangents[i]
        if np.linalg.norm(t_in) == 0.0 or np.linalg.norm(t_out) == 0.0:
            continue
        cosine = float(np.dot(t_in, t_out))
        angles[i - 1] = np.arccos(np.clip(cosine, -1.0, 1.0))
    return angles
