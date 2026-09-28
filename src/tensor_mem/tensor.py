"""Tensor type primitives: dtypes, shapes, aligned byte sizing, descriptors.

A :class:`TensorType` is the static contract a graph edge carries. A
:class:`TensorMeta` adds a concrete shape instance (dynamic execution may bind
a different concrete shape as long as rank and dtype agree and capacity is
replanned).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import InputValidationError

# Only a deliberately small dtype set is supported by the restricted runtime.
_SUPPORTED_DTYPES: dict[str, type[np.generic]] = {
    "float32": np.float32,
    "float64": np.float64,
    "int32": np.int32,
    "int64": np.int64,
}

# itemsize lookup kept independent of numpy dispatch for clear error messages.
_ITEMSIZE: dict[str, int] = {
    "float32": 4,
    "float64": 8,
    "int32": 4,
    "int64": 8,
}

DEFAULT_ALIGNMENT = 64  # bytes; cache-line / SIMD-friendly default.
MIN_ALIGNMENT = 1


@dataclass(frozen=True)
class TensorType:
    """Static tensor type: element dtype plus rank (number of dimensions)."""

    dtype: str
    rank: int

    def __post_init__(self) -> None:
        if self.dtype not in _ITEMSIZE:
            raise InputValidationError(
                f"unsupported dtype {self.dtype!r}",
                dtype=self.dtype,
                supported=sorted(_ITEMSIZE),
            )
        if self.rank < 0:
            raise InputValidationError("rank must be non-negative", rank=self.rank)

    @property
    def itemsize(self) -> int:
        return _ITEMSIZE[self.dtype]

    @property
    def numpy_dtype(self) -> np.dtype[Any]:
        return np.dtype(_SUPPORTED_DTYPES[self.dtype])

    def compatible_with(self, shape: "Shape") -> bool:
        return len(shape.dims) == self.rank


@dataclass(frozen=True)
class Shape:
    """Concrete tensor shape with positive int dims."""

    dims: tuple[int, ...]

    def __init__(self, *dims: int) -> None:
        # frozen dataclass with variadic dims needs object.__setattr__
        cleaned: list[int] = []
        for d in dims:
            if isinstance(d, bool) or not isinstance(d, int):
                raise InputValidationError(
                    "shape dims must be ints", dims=list(dims), got=repr(d)
                )
            if d <= 0:
                raise InputValidationError(
                    "shape dims must be positive integers", dims=list(dims)
                )
            cleaned.append(d)
        object.__setattr__(self, "dims", tuple(cleaned))

    @property
    def rank(self) -> int:
        return len(self.dims)

    @property
    def numel(self) -> int:
        n = 1
        for d in self.dims:
            n *= d
        return n

    def with_dim(self, index: int, value: int) -> "Shape":
        new_dims = list(self.dims)
        new_dims[index] = value
        return Shape(*new_dims)

    def as_tuple(self) -> tuple[int, ...]:
        return self.dims

    def __iter__(self):  # convenience: Shape(2, 3) -> (2, 3)
        return iter(self.dims)

    def __str__(self) -> str:
        return "x".join(str(d) for d in self.dims)


def shape_from(value: Any) -> Shape:
    """Coerce lists/tuples/Shapes into a :class:`Shape`, validating input."""
    if isinstance(value, Shape):
        return value
    if isinstance(value, (list, tuple)):
        if not value:
            raise InputValidationError("shape must have at least one dim", shape=value)
        return Shape(*value)
    raise InputValidationError(
        "cannot interpret value as shape", value=repr(value)
    )


def byte_size(shape: Shape, dtype: str) -> int:
    """Exact unaligned byte count for a shaped tensor."""
    if dtype not in _ITEMSIZE:
        raise InputValidationError("unsupported dtype", dtype=dtype)
    return shape.numel * _ITEMSIZE[dtype]


def align_up(value: int, alignment: int) -> int:
    """Round ``value`` up to a multiple of ``alignment`` bytes."""
    if alignment < MIN_ALIGNMENT:
        raise InputValidationError(
            "alignment must be >= 1", alignment=alignment
        )
    return math.ceil(value / alignment) * alignment


def aligned_byte_size(shape: Shape, dtype: str, alignment: int = DEFAULT_ALIGNMENT) -> int:
    """Aligned footprint of a tensor; alignment padding counts toward peak."""
    return align_up(byte_size(shape, dtype), alignment)


def supported_dtypes() -> list[str]:
    return sorted(_ITEMSIZE)


@dataclass(frozen=True)
class TensorMeta:
    """A tensor type plus a concrete shape (a realized edge)."""

    tensor_type: TensorType
    shape: Shape

    def __post_init__(self) -> None:
        if self.tensor_type.rank != self.shape.rank:
            raise InputValidationError(
                "rank mismatch between type and shape",
                dtype=self.tensor_type.dtype,
                type_rank=self.tensor_type.rank,
                shape_rank=self.shape.rank,
            )

    @property
    def dtype(self) -> str:
        return self.tensor_type.dtype

    @property
    def numel(self) -> int:
        return self.shape.numel

    def bytes(self) -> int:
        return byte_size(self.shape, self.tensor_type.dtype)

    def aligned_bytes(self, alignment: int = DEFAULT_ALIGNMENT) -> int:
        return aligned_byte_size(self.shape, self.tensor_type.dtype, alignment)
