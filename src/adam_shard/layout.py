"""Deterministic flattened layout over named parameter tensors.

Parameters are concatenated in *sorted-name* order into one 1-D vector.  The
mapping is fully described by (name, shape, offset, numel) records, so a
receiver rebuilds tensors by identity rather than by the sender's dict
iteration order.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .tensor_types import TensorId


@dataclass(frozen=True)
class ParamLayout:
    ids: tuple[TensorId, ...]

    def __post_init__(self) -> None:
        shapes_by_name: dict[str, tuple[int, ...]] = {}
        for tid in self.ids:
            if tid.name in shapes_by_name:
                raise ValueError(f"duplicate parameter name {tid.name!r}")
            shapes_by_name[tid.name] = tid.shape

    @property
    def total_numel(self) -> int:
        return sum(i.numel for i in self.ids)

    def ranges(self) -> dict[str, tuple[int, int, tuple[int, ...]]]:
        """name -> (start, end, shape) over the flat vector, sorted-name order."""

        out: dict[str, tuple[int, int, tuple[int, ...]]] = {}
        cursor = 0
        for tid in sorted(self.ids, key=lambda t: t.name):
            out[tid.name] = (cursor, cursor + tid.numel, tid.shape)
            cursor += tid.numel
        return out

    def flatten(self, params: dict[str, np.ndarray]) -> np.ndarray:
        flat = np.empty(self.total_numel, dtype=np.result_type(*params.values()) if params else np.float64)
        for name, (start, end, _) in self.ranges().items():
            if name not in params:
                raise KeyError(f"layout flatten missing parameter {name!r}")
            arr = np.asarray(params[name]).reshape(-1)
            if arr.shape[0] != end - start:
                raise ValueError(f"parameter {name!r} numel mismatch: {arr.shape[0]} != {end - start}")
            flat[start:end] = arr
        return flat

    def unflatten(self, flat: np.ndarray) -> dict[str, np.ndarray]:
        flat = np.asarray(flat).reshape(-1)
        if flat.shape[0] != self.total_numel:
            raise ValueError(f"flat size {flat.shape[0]} != layout size {self.total_numel}")
        out: dict[str, np.ndarray] = {}
        for name, (start, end, shape) in self.ranges().items():
            out[name] = flat[start:end].reshape(shape).copy()
        return out

    def to_manifest(self) -> list[dict]:
        return [
            {"name": name, "shape": list(shape), "offset": start, "numel": end - start}
            for name, (start, end, shape) in self.ranges().items()
        ]

    @classmethod
    def from_manifest(cls, records: list[dict]) -> "ParamLayout":
        ids = tuple(
            TensorId(name=str(r["name"]), shape=tuple(int(d) for d in r["shape"])) for r in records
        )
        layout = cls(ids=ids)
        # Offsets must describe a contiguous, gap-free sorted-name packing.
        cursor = 0
        for r in records:
            if int(r["offset"]) != cursor or int(r["numel"]) <= 0:
                raise ValueError("layout manifest is not a contiguous packing")
            cursor = int(r["offset"]) + int(r["numel"])
        if cursor != layout.total_numel:
            raise ValueError("layout manifest length does not match parameter shapes")
        return layout
