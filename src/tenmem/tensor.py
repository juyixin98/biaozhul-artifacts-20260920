"""Tensor value model: dtype, dynamic shape, aligned byte sizing and aliases.

A :class:`TensorSpec` is the *symbolic* description of a value (known at plan
time).  Shapes may contain a dimension bound ``(min, max)``: the planner always
sizes buffers for the *upper* bound (worst case capacity) while the executor
records the *actual* runtime shape.  When an actual shape exceeds the bound the
allocation is invalid by construction and the executor raises instead of
silently writing past the buffer (no out-of-bound reuse).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Bytes per element for the supported dtypes.
ITEMSIZE: dict[str, int] = {
    "float32": 4,
    "float64": 8,
    "int32": 4,
    "int64": 8,
}

# Default SIMD-friendly alignment in bytes.
DEFAULT_ALIGNMENT = 64


def align_up(offset: int, alignment: int) -> int:
    """Round ``offset`` up to a multiple of ``alignment``."""
    if alignment <= 0:
        return offset
    return (offset + alignment - 1) // alignment * alignment


@dataclass(frozen=True)
class DimBound:
    """A dimension with inclusive ``[lo, hi]`` bounds; equal for static dims."""

    lo: int
    hi: int

    @classmethod
    def fixed(cls, value: int) -> "DimBound":
        return cls(int(value), int(value))

    @property
    def is_dynamic(self) -> bool:
        return self.lo != self.hi


@dataclass(frozen=True)
class TensorSpec:
    """Symbolic tensor description.

    ``shape`` entries are plain ints (static) or ``(min, max)`` pairs /
    :class:`DimBound` for dynamic dimensions.
    """

    name: str
    shape: tuple[DimBound | int, ...]
    dtype: str = "float32"

    def __post_init__(self) -> None:
        if self.dtype not in ITEMSIZE:
            raise ValueError(f"unsupported dtype {self.dtype!r}")
        dims = []
        for d in self.shape:
            if isinstance(d, DimBound):
                bound = d
            elif isinstance(d, (tuple, list)) and len(d) == 2:
                bound = DimBound(int(d[0]), int(d[1]))
            else:
                bound = DimBound.fixed(d)
            if bound.lo < 0 or bound.hi < bound.lo:
                raise ValueError(f"invalid dimension bound for {self.name!r}: {d!r}")
            dims.append(bound)
        object.__setattr__(self, "shape", tuple(dims))

    @property
    def bounds(self) -> tuple[DimBound, ...]:
        return self.shape  # type: ignore[return-value]

    @property
    def max_shape(self) -> tuple[int, ...]:
        return tuple(d.hi for d in self.bounds)

    @property
    def min_shape(self) -> tuple[int, ...]:
        return tuple(d.lo for d in self.bounds)

    @property
    def is_dynamic(self) -> bool:
        return any(d.is_dynamic for d in self.bounds)

    @property
    def itemsize(self) -> int:
        return ITEMSIZE[self.dtype]

    def num_elements(self, shape: tuple[int, ...]) -> int:
        int_shape = tuple(int(s) for s in shape)
        return int(np.prod(int_shape, dtype=np.int64)) if int_shape else 1

    def bytes_for(self, shape: tuple[int, ...]) -> int:
        return self.num_elements(shape) * self.itemsize

    @property
    def max_bytes(self) -> int:
        return self.bytes_for(self.max_shape)

    def check_runtime_shape(self, shape: tuple[int, ...]) -> None:
        """Raise ``ValueError`` if a concrete shape violates the declared bound."""
        shape = tuple(int(s) for s in shape)
        if len(shape) != len(self.bounds):
            raise ValueError(
                f"rank mismatch for {self.name!r}: spec rank {len(self.bounds)}, "
                f"got {len(shape)}"
            )
        for axis, (actual, bound) in enumerate(zip(shape, self.bounds)):
            if actual < bound.lo or actual > bound.hi:
                raise ValueError(
                    f"shape out of declared bound for {self.name!r} axis {axis}: "
                    f"actual {actual} not in [{bound.lo}, {bound.hi}]"
                )

    def to_dict(self) -> dict:
        dims = [d.hi if not d.is_dynamic else [d.lo, d.hi] for d in self.bounds]
        return {"name": self.name, "shape": dims, "dtype": self.dtype}

    @classmethod
    def from_dict(cls, data: dict) -> "TensorSpec":
        return cls(name=str(data["name"]), shape=tuple(data["shape"]), dtype=str(data.get("dtype", "float32")))


@dataclass(frozen=True)
class Buffer:
    """A planned backing allocation.

    ``capacity`` is aligned and sized for the worst-case spec assigned to it.
    ``owners`` lists every tensor that may occupy the buffer (buffer reuse /
    alias union).  Two buffers never overlap; reuse means *sequential* sharing.
    """

    id: int
    capacity: int
    alignment: int
    owners: tuple[str, ...] = field(default_factory=tuple)
    workspace: bool = False
    reusable: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "capacity": self.capacity,
            "alignment": self.alignment,
            "owners": list(self.owners),
            "workspace": self.workspace,
            "reusable": self.reusable,
        }
