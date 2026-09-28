"""Fixed bucket layout over the parameter graph.

Gradients are packed into one flat vector per parameter (in graph order),
and the concatenation of those flats is sliced into contiguous buckets of
at most ``bucket_size`` scalars.  The layout is computed **once** from the
graph and the bucket size; it does not depend on the round, the workers,
or which parameters happen to be unused in a given round.  Non-trainable
parameters occupy their normal slots as explicit zero placeholders, so the
layout never shifts when a parameter is frozen.

This module owns *where every scalar lives*.  It does not know about
rounds, workers, or reduction semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np

from bucket_sync.graph import ParameterGraph


class BucketLayoutError(ValueError):
    """Raised when a bucket layout is invalid or a value does not fit it."""


@dataclass(frozen=True)
class ParamSlice:
    """Where one parameter lives inside the flat concatenated vector."""

    param: str
    offset: int  # start index in the flat vector
    size: int
    trainable: bool


@dataclass(frozen=True)
class Bucket:
    """A contiguous half-open range ``[start, end)`` of the flat vector."""

    index: int
    start: int
    end: int

    @property
    def size(self) -> int:
        return self.end - self.start


class BucketLayout:
    """Immutable mapping between parameters, the flat vector, and buckets."""

    def __init__(self, graph: ParameterGraph, bucket_size: int) -> None:
        if bucket_size <= 0:
            raise BucketLayoutError(
                f"bucket_size must be a positive int, got {bucket_size!r}"
            )
        self._graph = graph
        self._bucket_size = int(bucket_size)

        slices: List[ParamSlice] = []
        offset = 0
        for node in graph:
            slices.append(
                ParamSlice(
                    param=node.spec.name,
                    offset=offset,
                    size=node.spec.size,
                    trainable=node.trainable,
                )
            )
            offset += node.spec.size
        self._flat_size = offset
        self._slices: Tuple[ParamSlice, ...] = tuple(slices)
        self._by_param: Dict[str, ParamSlice] = {s.param: s for s in slices}

        buckets: List[Bucket] = []
        start = 0
        while start < self._flat_size:
            end = min(start + self._bucket_size, self._flat_size)
            buckets.append(Bucket(index=len(buckets), start=start, end=end))
            start = end
        self._buckets: Tuple[Bucket, ...] = tuple(buckets)

    # ---- structure -------------------------------------------------

    @property
    def graph(self) -> ParameterGraph:
        return self._graph

    @property
    def bucket_size(self) -> int:
        return self._bucket_size

    @property
    def flat_size(self) -> int:
        return self._flat_size

    @property
    def buckets(self) -> Tuple[Bucket, ...]:
        return self._buckets

    @property
    def param_slices(self) -> Tuple[ParamSlice, ...]:
        return self._slices

    def bucket_count(self) -> int:
        return len(self._buckets)

    def slice_for(self, param: str) -> ParamSlice:
        try:
            return self._by_param[param]
        except KeyError:
            raise BucketLayoutError(
                f"parameter {param!r} is not in graph {self._graph.name!r}"
            ) from None

    def fingerprint(self) -> str:
        bounds = ",".join(f"[{b.start},{b.end})" for b in self._buckets)
        return f"{self._graph.fingerprint()}|bucket_size={self._bucket_size}|{bounds}"

    # ---- packing ---------------------------------------------------

    def zeros_flat(self) -> np.ndarray:
        return np.zeros(self._flat_size, dtype=np.float64)

    def pack_flat(
        self,
        values: Dict[str, np.ndarray],
        *,
        strict: bool = True,
    ) -> np.ndarray:
        """Pack per-parameter arrays into the flat vector.

        Missing parameters are filled with their explicit zero placeholder
        (they keep their slot; the slot is simply zero).  With
        ``strict=True``, any *unexpected* key is rejected so a typo'd or
        stale parameter name cannot be silently dropped.
        """
        expected = set(self._graph.param_names())
        unknown = set(values) - expected
        if strict and unknown:
            raise BucketLayoutError(
                f"pack_flat got unknown parameters {sorted(unknown)!r}; "
                f"graph knows {sorted(expected)!r}"
            )
        flat = self.zeros_flat()
        for ps in self._slices:
            if ps.param not in values:
                continue  # explicit placeholder: slot stays zero
            spec = self._graph.spec(ps.param)
            arr = np.asarray(values[ps.param], dtype=np.float64).reshape(-1)
            if arr.size != ps.size:
                raise BucketLayoutError(
                    f"parameter {ps.param!r}: expected {ps.size} elements "
                    f"(shape {spec.shape}), got {arr.size}"
                )
            flat[ps.offset : ps.offset + ps.size] = arr
        return flat

    def unpack_flat(self, flat: np.ndarray) -> Dict[str, np.ndarray]:
        """Inverse of :meth:`pack_flat`; every parameter gets its array."""
        arr = np.asarray(flat, dtype=np.float64).reshape(-1)
        if arr.size != self._flat_size:
            raise BucketLayoutError(
                f"flat vector has {arr.size} elements, layout expects "
                f"{self._flat_size}"
            )
        out: Dict[str, np.ndarray] = {}
        for ps in self._slices:
            spec = self._graph.spec(ps.param)
            out[ps.param] = arr[ps.offset : ps.offset + ps.size].reshape(spec.shape)
        return out

    def placeholder_mask(self) -> np.ndarray:
        """Boolean flat mask that is True exactly on placeholder (non-trainable) slots."""
        mask = np.zeros(self._flat_size, dtype=bool)
        for ps in self._slices:
            if not ps.trainable:
                mask[ps.offset : ps.offset + ps.size] = True
        return mask

    def bucket(self, index: int) -> Bucket:
        """Look up one bucket by index."""
        if not 0 <= index < len(self._buckets):
            raise BucketLayoutError(
                f"bucket index {index} out of range [0, {len(self._buckets)})"
            )
        return self._buckets[index]


def plan_buckets(graph: ParameterGraph, bucket_size: int) -> BucketLayout:
    """Build the fixed layout for a job.  Thin wrapper for readability."""
    return BucketLayout(graph, bucket_size)
