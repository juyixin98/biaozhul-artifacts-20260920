"""Graph store and training state.

Each registered graph is immutable once created. The mutable *training
state* is the current parameter point (e.g. model weights being iterated
by an outer training loop). Every point update bumps ``point_version``;
HVP requests that reference the stored point may pin ``expected_version``
for optimistic concurrency — a mismatch is a ``state_conflict`` error, so
a caller never silently computes an HVP at a stale iterate.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass

import numpy as np

from .errors import not_found, state_conflict
from .graph import Graph


@dataclass
class StoredGraph:
    graph: Graph
    created_at: float
    point: np.ndarray | None = None
    point_version: int = 0


class GraphStore:
    def __init__(self) -> None:
        self._graphs: dict[str, StoredGraph] = {}
        self._lock = threading.Lock()

    def create(self, graph: Graph) -> str:
        graph_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._graphs[graph_id] = StoredGraph(graph=graph, created_at=time.time())
        return graph_id

    def get(self, graph_id: str) -> StoredGraph:
        with self._lock:
            stored = self._graphs.get(graph_id)
        if stored is None:
            raise not_found("unknown graph id", graph_id=graph_id)
        return stored

    def set_point(self, graph_id: str, point: np.ndarray) -> int:
        with self._lock:
            stored = self._graphs.get(graph_id)
            if stored is None:
                raise not_found("unknown graph id", graph_id=graph_id)
            stored.point = np.asarray(point, dtype=np.float64).copy()
            stored.point_version += 1
            return stored.point_version

    def stored_point(self, graph_id: str, expected_version: int | None) -> tuple[np.ndarray, int]:
        stored = self.get(graph_id)
        if expected_version is not None and expected_version != stored.point_version:
            raise state_conflict(
                "stored point version mismatch: the training state changed since "
                "the caller last observed it; re-read the point and retry",
                graph_id=graph_id,
                expected_version=expected_version,
                actual_version=stored.point_version,
            )
        if stored.point is None:
            raise state_conflict(
                "no stored point for this graph; set one before using use_stored_point",
                graph_id=graph_id,
            )
        return stored.point, stored.point_version

    def delete(self, graph_id: str) -> None:
        with self._lock:
            if self._graphs.pop(graph_id, None) is None:
                raise not_found("unknown graph id", graph_id=graph_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._graphs)
