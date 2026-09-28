"""Owned element storage and reference tracking.

Storage is a contiguous ``float64`` buffer.  Tensors are views over storage;
the storage object remembers every live view so aliasing relationships can be
reported and overlapping writes can be detected precisely.
"""
from __future__ import annotations

import itertools
from typing import Iterator

import numpy as np

from .layout import address_bounds

_storage_counter = itertools.count(1)


class Storage:
    """A contiguous owned float64 buffer plus its open view intervals."""

    __slots__ = ("buffer", "storage_id", "_views")

    def __init__(self, buffer: np.ndarray, storage_id: int | None = None) -> None:
        if buffer.dtype != np.float64:
            from .errors import DTypeError

            raise DTypeError(f"only float64 storage is supported, got {buffer.dtype}")
        if not buffer.flags.c_contiguous:
            # Defensive: storage must own a dense C-order buffer.
            buffer = np.ascontiguousarray(buffer, dtype=np.float64)
        self.buffer = buffer
        self.storage_id = storage_id if storage_id is not None else next(_storage_counter)
        # Maps tensor_id -> inclusive (lo, hi) storage interval.
        self._views: dict[int, tuple[int, int]] = {}

    @property
    def size(self) -> int:
        return int(self.buffer.size)

    def register_view(self, tensor_id: int, shape, strides, offset: int) -> tuple[int, int]:
        lo, hi = address_bounds(shape, strides, offset)
        self._views[tensor_id] = (lo, hi)
        return lo, hi

    def unregister_view(self, tensor_id: int) -> None:
        self._views.pop(tensor_id, None)

    def view_intervals(self) -> dict[int, tuple[int, int]]:
        return dict(self._views)

    def overlaps_any(
        self,
        lo: int,
        hi: int,
        *,
        exclude: int | None = None,
        writable: bool = False,
    ) -> Iterator[tuple[int, tuple[int, int]]]:
        """Yield registered views whose storage interval overlaps [lo, hi].

        ``writable`` restricts the check to views that own more than one
        distinct storage position (read-only broadcasts of a scalar never
        conflict), but since we only use this for write *sources*, a source
        interval always comes from a real element range.
        """
        for tid, (vlo, vhi) in self._views.items():
            if tid == exclude:
                continue
            if lo <= vhi and vlo <= hi:
                yield tid, (vlo, vhi)

    @classmethod
    def allocate(cls, values: np.ndarray) -> "Storage":
        # Force a copy even when the input is already contiguous: owned storage
        # must never alias caller memory (that would corrupt test oracles).
        flat = np.array(np.asarray(values, dtype=np.float64), order="C", copy=True).reshape(-1)
        return cls(flat)
