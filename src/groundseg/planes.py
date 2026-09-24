"""平面几何与（带种子的）RANSAC。

平面方程统一写成  n·x + d = 0，n 为单位法向量，并把法向定向为 +Z 朝上
（n[2] >= 0），这样倾角只需要一个点积就能计算。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Plane:
    normal: np.ndarray  # shape (3,)，单位向量，n[2] >= 0
    offset: float       # n·x + offset = 0

    def signed_distance(self, points: np.ndarray) -> np.ndarray:
        return points @ self.normal + self.offset

    def tilt_deg(self) -> float:
        """法向与竖直 +Z 的夹角（度），范围 [0, 90]。"""
        cos_tilt = float(np.clip(self.normal[2], -1.0, 1.0))
        return float(np.degrees(np.arccos(cos_tilt)))


def _orient_up(normal: np.ndarray) -> np.ndarray:
    return normal if normal[2] >= 0.0 else -normal


def fit_plane_svd(points: np.ndarray) -> Plane | None:
    """用最小二乘（SVD 最小奇异向量）拟合平面。

    点共线/共点（矩阵秩不足）时返回 None。
    """
    if points.shape[0] < 3:
        return None
    centroid = points.mean(axis=0)
    centered = points - centroid
    # 最小奇异向量即法向；小奇异值全部接近 0 说明点共线。
    _, s, vt = np.linalg.svd(centered, full_matrices=False)
    if s.shape[0] < 3 or s[1] <= 1e-9 * max(s[0], 1e-12):
        return None
    normal = _orient_up(vt[2])
    offset = float(-normal @ centroid)
    return Plane(normal=normal, offset=offset)


def plane_from_three(p1: np.ndarray, p2: np.ndarray, p3: np.ndarray) -> Plane | None:
    """三点定面；三点共线返回 None。"""
    n = np.cross(p2 - p1, p3 - p1)
    norm = float(np.linalg.norm(n))
    if norm <= 1e-12:
        return None
    n = _orient_up(n / norm)
    return Plane(normal=n, offset=float(-n @ p1))


@dataclass(frozen=True)
class RansacPlaneResult:
    plane: Plane | None
    inlier_mask: np.ndarray            # bool，对传入 points
    inlier_ratio: float
    iterations_used: int
    reason: str                        # "ok" / "too_few_points" / "degenerate" / "no_model"


def ransac_plane(
    points: np.ndarray,
    *,
    iterations: int,
    distance_threshold: float,
    rng: np.random.Generator,
    seed_indices: np.ndarray | None = None,
    unique_scale: float = 1e-9,
) -> RansacPlaneResult:
    """带种子的 RANSAC 平面拟合。

    种子机制（确定性，无随机采样失败风险）：
      * 若提供 seed_indices（去重后 >=3 个点），**第一个**模型假设直接取
        其中前 3 个点；剩余种子点仍会在后续迭代中以高概率被采到——
        每次迭代有 50% 概率从种子池采点，50% 概率全局均匀采样。
      * 其余假设由给定 rng 均匀采样 3 点，三点共线则重试。

    找到内点最多的假设后，对全部内点做一次 SVD 最小二乘重拟合，得到
    更稳定的最终平面（重拟合仍可能因内点共线而失败，此时保留原假设）。
    """
    n_pts = points.shape[0]
    mask = np.zeros(n_pts, dtype=bool)

    if n_pts < 3:
        return RansacPlaneResult(None, mask, 0.0, 0, "too_few_points")

    # 构造采样池：把数值上重复的点折叠，避免 RANSAC 反复抽到相同三点。
    # 用“去重后的代表点”采样，但内点统计仍对原始全部点进行。
    if unique_scale > 0:
        keys = np.round(points / max(unique_scale, 1e-12)).astype(np.int64)
        _, unique_idx = np.unique(keys, axis=0, return_index=True)
    else:
        unique_idx = np.arange(n_pts)
    if unique_idx.shape[0] < 3:
        return RansacPlaneResult(None, mask, 0.0, 0, "degenerate")

    seed_idx: np.ndarray | None = None
    if seed_indices is not None and seed_indices.size >= 3:
        # 只保留落在当前点集内的合法种子
        valid = seed_indices[(seed_indices >= 0) & (seed_indices < n_pts)]
        if valid.size >= 3:
            seed_idx = np.unique(valid)

    best_inliers = mask
    best_count = 0
    best_plane: Plane | None = None
    used = 0

    def try_hypothesis(ids) -> tuple[Plane | None, np.ndarray, int]:
        plane = plane_from_three(points[ids[0]], points[ids[1]], points[ids[2]])
        if plane is None:
            return None, mask, 0
        inl = np.abs(plane.signed_distance(points)) <= distance_threshold
        return plane, inl, int(inl.sum())

    # 第 0 个假设：显式先验种子的前三点。
    first_used_seed = False
    if seed_idx is not None and seed_idx.size >= 3:
        plane, inl, count = try_hypothesis(seed_idx[:3])
        used += 1
        if plane is not None and count > best_count:
            best_plane, best_inliers, best_count = plane, inl, count
        first_used_seed = True

    # 采样池：种子（若有）+ 全部唯一点
    pool = unique_idx
    seed_pool = None
    if seed_idx is not None and seed_idx.size >= 3:
        seed_pool = np.intersect1d(seed_idx, unique_idx)
        if seed_pool.size < 3:
            seed_pool = None

    attempts = iterations - used
    for _ in range(attempts):
        used += 1
        if seed_pool is not None and rng.random() < 0.5:
            ids = rng.choice(seed_pool, size=3, replace=False)
        else:
            ids = rng.choice(pool, size=3, replace=False)
        plane, inl, count = try_hypothesis(ids)
        if plane is None:
            continue
        if count > best_count:
            best_plane, best_inliers, best_count = plane, inl, count

    if best_plane is None:
        return RansacPlaneResult(None, mask, 0.0, used, "no_model")

    # 用全部内点 SVD 重拟合；退化时保留 RANSAC 假设平面。
    refit = fit_plane_svd(points[best_inliers])
    if refit is not None:
        refit_inliers = np.abs(refit.signed_distance(points)) <= distance_threshold
        if int(refit_inliers.sum()) >= best_count:
            best_plane = refit
            best_inliers = refit_inliers
            best_count = int(refit_inliers.sum())

    ratio = best_count / n_pts
    return RansacPlaneResult(best_plane, best_inliers, ratio, used, "ok")
