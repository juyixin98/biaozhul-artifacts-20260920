"""A forest of TF edges with time-aware lookup and chain composition.

The tree stores directed edges ``parent -> child``; each edge holds a
:class:`TransformBuffer` of timestamped SE(3) samples giving the pose of the
child frame expressed in the parent frame (``T_parent_child``).

Invariants:
  * every frame has at most one parent, so the structure is a forest and
    cycles are impossible -- inserting an edge whose child already has a
    parent (or whose parent equals the child) raises :class:`CycleError`;
  * lookups walk the unique undirected path between two frames, composing
    edge transforms and inverting edges traversed against their direction.

``lookup(source, target, time)`` returns ``T_target_source``: the transform
that maps points expressed in ``source`` into ``target`` coordinates.
"""

from __future__ import annotations

from collections import deque

from .buffer import TransformBuffer
from .exceptions import ConnectivityError, CycleError, LookupError_
from .transform import SE3


class TransformTree:
    def __init__(self) -> None:
        # child frame -> parent frame
        self._parents: dict[str, str] = {}
        # (parent, child) -> time series of T_parent_child
        self._buffers: dict[tuple[str, str], TransformBuffer] = {}

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    def set_transform(
        self, parent: str, child: str, time: float, transform: SE3
    ) -> None:
        """Add a timestamped sample to edge ``parent -> child``.

        Raises CycleError if the edge would close a loop or give ``child``
        a second parent.
        """
        self._validate_frame_name(parent)
        self._validate_frame_name(child)
        if parent == child:
            raise CycleError(f"self-edge {parent!r} -> {child!r} is not allowed")
        existing = self._parents.get(child)
        if existing is not None and existing != parent:
            raise CycleError(
                f"frame {child!r} already has parent {existing!r}; "
                f"adding {parent!r} -> {child!r} would create a cycle"
            )
        # Walk up the ancestor chain of `parent`: if `child` is among them,
        # the new edge closes a loop (e.g. a->b, b->c, then c->a).
        node = parent
        while node in self._parents:
            node = self._parents[node]
            if node == child:
                raise CycleError(
                    f"adding {parent!r} -> {child!r} would create a cycle"
                )
        key = (parent, child)
        buffer = self._buffers.get(key)
        if buffer is None:
            buffer = TransformBuffer()
            self._buffers[key] = buffer
        buffer.insert(time, transform)
        self._parents[child] = parent

    @staticmethod
    def _validate_frame_name(name: str) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("frame names must be non-empty strings")

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def frames(self) -> list[str]:
        names = set(self._parents) | set(self._parents.values())
        return sorted(names)

    @property
    def edges(self) -> list[tuple[str, str]]:
        return sorted(self._buffers)

    def has_frame(self, name: str) -> bool:
        return name in self._parents or name in self._parents.values()

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------
    def lookup(self, source: str, target: str, time: float) -> SE3:
        """Return T_target_source at ``time`` (maps source points to target).

        Raises ConnectivityError if no chain connects the frames,
        LookupError_ if an edge on the chain has no data, and
        ExtrapolationError if ``time`` falls outside any edge's range.
        """
        if source == target:
            return SE3.identity()
        path = self._find_path(source, target)
        if path is None:
            raise ConnectivityError(
                f"no transform chain connects {source!r} to {target!r}"
            )
        result = SE3.identity()
        for a, b in zip(path, path[1:]):
            result = self._transform_between(a, b, time) * result
        return result

    def _transform_between(self, a: str, b: str, time: float) -> SE3:
        """Return T_b_a for adjacent frames a, b (either edge direction)."""
        forward = self._buffers.get((a, b))
        if forward is not None:
            # Edge a -> b stores T_a_b; we need T_b_a.
            return forward.lookup(time).inverse()
        backward = self._buffers.get((b, a))
        if backward is not None:
            return backward.lookup(time)
        raise LookupError_(f"no edge between {a!r} and {b!r}")  # pragma: no cover

    def _find_path(self, source: str, target: str) -> list[str] | None:
        """BFS over the undirected edge graph; returns frames source..target."""
        adjacency: dict[str, list[str]] = {}
        for parent, child in self._buffers:
            adjacency.setdefault(parent, []).append(child)
            adjacency.setdefault(child, []).append(parent)
        if source not in adjacency or target not in adjacency:
            return None
        prev: dict[str, str | None] = {source: None}
        queue: deque[str] = deque([source])
        while queue:
            node = queue.popleft()
            if node == target:
                break
            for nxt in adjacency[node]:
                if nxt not in prev:
                    prev[nxt] = node
                    queue.append(nxt)
        if target not in prev:
            return None
        path = [target]
        while path[-1] != source:
            path.append(prev[path[-1]])  # type: ignore[arg-type]
        path.reverse()
        return path
