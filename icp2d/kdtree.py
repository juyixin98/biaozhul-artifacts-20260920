"""Minimal 2D KD-tree for nearest-neighbour queries (NumPy only).

ICP needs an exact nearest-neighbour search against the target scan at every
iteration. A brute-force O(N*M) pass is the reference implementation; this
KD-tree provides the same results in O((N+M) log M) time. The test-suite
cross-checks the tree against brute force on random data.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class _Node:
    index: int
    point: np.ndarray
    axis: int
    left: "_Node | None"
    right: "_Node | None"


class KDTree2D:
    """Static 2D KD-tree built once from the target scan."""

    def __init__(self, points: np.ndarray):
        points = np.asarray(points, dtype=float)
        if points.ndim != 2 or points.shape[1] != 2:
            raise ValueError("KDTree2D expects an (N, 2) array")
        if len(points) == 0:
            raise ValueError("KDTree2D cannot be built from an empty cloud")
        self._points = points
        self._root = self._build(np.arange(len(points)), depth=0)

    def _build(self, indices: np.ndarray, depth: int) -> _Node | None:
        if len(indices) == 0:
            return None
        axis = depth % 2
        order = np.argsort(self._points[indices, axis], kind="stable")
        mid = len(order) // 2
        node_index = int(indices[order[mid]])
        return _Node(
            index=node_index,
            point=self._points[node_index],
            axis=axis,
            left=self._build(indices[order[:mid]], depth + 1),
            right=self._build(indices[order[mid + 1 :]], depth + 1),
        )

    def query(self, points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Return (distances, indices) of the nearest target point per query."""
        points = np.asarray(points, dtype=float)
        if points.ndim != 2 or points.shape[1] != 2:
            raise ValueError("query expects an (N, 2) array")
        distances = np.empty(len(points))
        indices = np.empty(len(points), dtype=int)
        for i, p in enumerate(points):
            best_dist, best_index = self._nearest(self._root, p, np.inf, -1)
            distances[i] = best_dist
            indices[i] = best_index
        return distances, indices

    def _nearest(
        self, node: _Node | None, p: np.ndarray, best_dist: float, best_index: int
    ) -> tuple[float, int]:
        if node is None:
            return best_dist, best_index
        dist = float(np.hypot(*(p - node.point)))
        if dist < best_dist:
            best_dist, best_index = dist, node.index
        axis = node.axis
        diff = p[axis] - node.point[axis]
        near, far = (node.left, node.right) if diff < 0 else (node.right, node.left)
        best_dist, best_index = self._nearest(near, p, best_dist, best_index)
        if abs(diff) < best_dist:
            best_dist, best_index = self._nearest(far, p, best_dist, best_index)
        return best_dist, best_index


def brute_force_nearest(
    query: np.ndarray, target: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """O(N*M) reference nearest-neighbour search (used in tests)."""
    query = np.asarray(query, dtype=float)
    target = np.asarray(target, dtype=float)
    diff = query[:, None, :] - target[None, :, :]
    dists = np.hypot(diff[..., 0], diff[..., 1])
    indices = np.argmin(dists, axis=1)
    return dists[np.arange(len(query)), indices], indices


# Below this many pairwise distances, the vectorized brute-force pass (a few
# NumPy kernel calls) is faster than per-point Python KD-tree traversals.
_BRUTE_FORCE_PAIR_LIMIT = 4_000_000


def nearest_neighbors(
    query: np.ndarray, target: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Exact nearest neighbour in ``target`` for each query point.

    Dispatches to the vectorized brute-force pass for small clouds and to
    the KD-tree for large ones; both return identical results.
    """
    query = np.asarray(query, dtype=float)
    target = np.asarray(target, dtype=float)
    if len(query) * len(target) <= _BRUTE_FORCE_PAIR_LIMIT:
        return brute_force_nearest(query, target)
    return KDTree2D(target).query(query)
