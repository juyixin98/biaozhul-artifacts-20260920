"""Tensor layout: binds flat vectors to named, shaped input slots.

The graph declares an ordered list of input slots (name + shape). Points,
direction vectors, gradients and HVP results are exchanged as flat float64
vectors whose length must equal ``Layout.size``; the layout is the single
source of truth for slicing them back into shaped tensors. A length or
finiteness mismatch is an input-validation error, never a silent reshape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np

from .errors import input_error


@dataclass(frozen=True)
class Slot:
    name: str
    shape: tuple[int, ...]
    offset: int
    size: int


class Layout:
    def __init__(self, slots: list[tuple[str, tuple[int, ...]]]) -> None:
        if not slots:
            raise input_error("layout must declare at least one input slot")
        seen: set[str] = set()
        built: list[Slot] = []
        offset = 0
        for name, shape in slots:
            if not isinstance(name, str) or not name:
                raise input_error("slot name must be a non-empty string", name=name)
            if name in seen:
                raise input_error("duplicate input slot name", name=name)
            seen.add(name)
            for dim in shape:
                if not isinstance(dim, int) or isinstance(dim, bool) or dim < 0:
                    raise input_error(
                        "slot shape dimensions must be non-negative integers",
                        name=name,
                        shape=list(shape),
                    )
            size = int(np.prod(shape, dtype=np.int64)) if shape else 1
            built.append(Slot(name=name, shape=tuple(shape), offset=offset, size=size))
            offset += size
        self._slots = tuple(built)
        self.size = offset

    @property
    def slots(self) -> tuple[Slot, ...]:
        return self._slots

    @property
    def names(self) -> list[str]:
        return [s.name for s in self._slots]

    def slot(self, name: str) -> Slot:
        for s in self._slots:
            if s.name == name:
                return s
        raise input_error("unknown slot name", name=name, known=self.names)

    def flatten(self, values: Mapping[str, np.ndarray]) -> np.ndarray:
        """Pack a {slot_name: shaped array} mapping into the flat layout order."""
        missing = [s.name for s in self._slots if s.name not in values]
        if missing:
            raise input_error("missing values for input slots", missing=missing)
        parts = []
        for s in self._slots:
            arr = np.asarray(values[s.name], dtype=np.float64)
            if arr.shape != s.shape:
                raise input_error(
                    "slot value has wrong shape",
                    name=s.name,
                    expected=list(s.shape),
                    actual=list(arr.shape),
                )
            parts.append(arr.reshape(-1))
        return np.concatenate(parts) if parts else np.zeros(0)

    def unflatten(self, vec: np.ndarray) -> dict[str, np.ndarray]:
        """Slice a flat vector back into {slot_name: shaped array}."""
        vec = np.asarray(vec, dtype=np.float64)
        if vec.ndim != 1 or vec.size != self.size:
            raise input_error(
                "flat vector does not match layout",
                expected_size=self.size,
                actual_size=int(vec.size),
            )
        return {
            s.name: vec[s.offset : s.offset + s.size].reshape(s.shape).copy()
            for s in self._slots
        }

    def to_dict(self) -> dict:
        return {
            "size": self.size,
            "slots": [
                {"name": s.name, "shape": list(s.shape), "offset": s.offset, "size": s.size}
                for s in self._slots
            ],
        }
