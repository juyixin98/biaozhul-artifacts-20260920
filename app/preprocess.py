"""输入预处理：重复点合并、特征尺度归一化。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class DedupResult:
    points: np.ndarray                 # 去重后的折线 (M, 2)
    index_map: list[int]               # 原索引 -> 去重后索引（长度 N）
    unique_count: int


def merge_consecutive_duplicates(points, tol: float) -> DedupResult:
    """合并连续重复点；非连续重复点（环/折返）保留。

    判定：与上一个保留点的欧氏距离 <= tol（tol 与坐标尺度相关）。
    index_map[k] 给出原始第 k 个点在去重序列中的位置，便于求解后恢复重复点。
    """
    pts = np.asarray(points, dtype=float)
    keep: list[int] = []
    index_map: list[int] = []
    for k in range(len(pts)):
        if not keep or np.linalg.norm(pts[k] - pts[keep[-1]]) > tol:
            keep.append(k)
        index_map.append(len(keep) - 1)
    return DedupResult(points=pts[keep].copy(), index_map=index_map, unique_count=len(keep))


def characteristic_length(points: np.ndarray) -> float:
    """特征尺度：包围盒对角线；退化（单点/共点）时回退 1.0。"""
    pts = np.asarray(points, dtype=float)
    lo = pts.min(axis=0)
    hi = pts.max(axis=0)
    diag = float(np.linalg.norm(hi - lo))
    return diag if diag > 1.0e-9 else 1.0


def expand_with_map(points_unique: np.ndarray, index_map: list[int]) -> np.ndarray:
    """按 index_map 把去重后的点列恢复为原始点数顺序。"""
    return np.asarray(points_unique, dtype=float)[np.asarray(index_map, dtype=int)]
