"""Tensor types and input layouts.

A :class:`TensorLayout` declares the name, shape and optional default of an
input variable. Variables are stored as flat NumPy arrays internally; the
layout maps between nested (shape-aware) values and the flat representation
and validates that user-supplied vectors (gradient/HVP directions) match the
declared layout exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .errors import InputError

_DTYPE = np.float64


def is_scalar_shape(shape: tuple[int, ...]) -> bool:
    return len(shape) == 0


@dataclass(frozen=True)
class TensorLayout:
    """Declaration of one input tensor."""

    name: str
    shape: tuple[int, ...]

    @property
    def size(self) -> int:
        return int(np.prod(self.shape)) if self.shape else 1

    @property
    def is_scalar(self) -> bool:
        return is_scalar_shape(self.shape)

    def flatten(self, value: Any) -> np.ndarray:
        """Coerce a JSON-ish value to a 1-D float64 array of ``size``."""
        arr = np.asarray(_to_float(value), dtype=_DTYPE)
        if self.is_scalar:
            if arr.ndim != 0:
                raise InputError(
                    f"variable {self.name!r} expects a scalar, got shape {arr.shape}",
                    detail={"variable": self.name, "expected_shape": list(self.shape),
                            "actual_shape": list(arr.shape)},
                )
            return arr.reshape(1)
        if arr.shape != tuple(self.shape):
            raise InputError(
                f"variable {self.name!r} expects shape {tuple(self.shape)}, "
                f"got {arr.shape}",
                detail={"variable": self.name, "expected_shape": list(self.shape),
                        "actual_shape": list(arr.shape)},
            )
        return arr.reshape(-1)

    def unflatten(self, flat: np.ndarray) -> Any:
        """Inverse of :meth:`flatten`; returns scalar or nested lists."""
        flat = np.asarray(flat, dtype=_DTYPE).reshape(-1)
        if flat.size != self.size:
            raise InputError(
                f"flat vector length {flat.size} does not match layout "
                f"{self.name!r} of size {self.size}",
                detail={"variable": self.name, "expected_size": self.size,
                        "actual_size": int(flat.size)},
            )
        if self.is_scalar:
            return float(flat[0])
        return flat.reshape(self.shape).tolist()


def _to_float(value: Any) -> Any:
    """Recursively convert ints/floats, rejecting bools and non-numerics."""
    if isinstance(value, bool):
        raise InputError("boolean is not a valid numeric value")
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_to_float(v) for v in value]
    if isinstance(value, np.ndarray):
        return value.astype(_DTYPE)
    raise InputError(f"unsupported numeric type: {type(value).__name__}")


@dataclass(frozen=True)
class InputLayout:
    """Ordered collection of tensor layouts defining the full input space."""

    variables: tuple[TensorLayout, ...]

    def __post_init__(self) -> None:
        names = [v.name for v in self.variables]
        if len(names) != len(set(names)):
            dup = sorted({n for n in names if names.count(n) > 1})
            raise InputError(f"duplicate variable names: {dup}",
                             detail={"duplicates": dup})

    @classmethod
    def from_specs(cls, specs: Iterable[dict[str, Any]]) -> "InputLayout":
        layouts: list[TensorLayout] = []
        for spec in specs:
            name = spec.get("name")
            if not isinstance(name, str) or not name:
                raise InputError("each variable requires a non-empty 'name'")
            shape_raw = spec.get("shape", [])
            shape = _validate_shape(shape_raw, name)
            layouts.append(TensorLayout(name=name, shape=shape))
        if not layouts:
            raise InputError("at least one input variable is required")
        return cls(tuple(layouts))

    def names(self) -> list[str]:
        return [v.name for v in self.variables]

    @property
    def total_size(self) -> int:
        return sum(v.size for v in self.variables)

    def index_of(self, name: str) -> int:
        offset = 0
        for v in self.variables:
            if v.name == name:
                return offset
            offset += v.size
        raise InputError(f"unknown variable {name!r}", detail={"variable": name})

    def offsets(self) -> dict[str, int]:
        """Flat start offset of each variable."""
        out: dict[str, int] = {}
        offset = 0
        for v in self.variables:
            out[v.name] = offset
            offset += v.size
        return out

    def layout(self, name: str) -> TensorLayout:
        for v in self.variables:
            if v.name == name:
                return v
        raise InputError(f"unknown variable {name!r}", detail={"variable": name})

    def pack(self, values: dict[str, Any]) -> np.ndarray:
        """Flatten ``{name: value}`` into one contiguous vector in layout order."""
        missing = [v.name for v in self.variables if v.name not in values]
        if missing:
            raise InputError(f"missing values for variables: {missing}",
                             detail={"missing": missing})
        chunks = [v.flatten(values[v.name]) for v in self.variables]
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=_DTYPE)

    def bind_vector(self, vectors: dict[str, Any], *, what: str) -> np.ndarray:
        """Validate and pack a per-variable direction/vector payload.

        Every declared variable must be present and each value must match the
        variable's shape: the vector is bound to the input layout.
        """
        if not isinstance(vectors, dict):
            raise InputError(f"{what} must be an object mapping name -> value")
        missing = [v.name for v in self.variables if v.name not in vectors]
        unknown = [k for k in vectors if k not in {v.name for v in self.variables}]
        if missing or unknown:
            raise InputError(
                f"{what} must cover exactly the declared variables",
                detail={"missing": missing, "unknown": unknown},
            )
        return self.pack(vectors)

    def unpack(self, flat: np.ndarray) -> dict[str, Any]:
        flat = np.asarray(flat, dtype=_DTYPE).reshape(-1)
        if flat.size != self.total_size:
            raise InputError(
                f"vector length {flat.size} does not match layout size "
                f"{self.total_size}",
                detail={"expected_size": self.total_size,
                        "actual_size": int(flat.size)},
            )
        out: dict[str, Any] = {}
        offset = 0
        for v in self.variables:
            out[v.name] = v.unflatten(flat[offset:offset + v.size])
            offset += v.size
        return out


def _validate_shape(shape_raw: Any, name: str) -> tuple[int, ...]:
    if isinstance(shape_raw, int):
        shape_raw = [shape_raw]
    if not isinstance(shape_raw, (list, tuple)):
        raise InputError(f"variable {name!r}: shape must be a list of ints")
    dims: list[int] = []
    for d in shape_raw:
        if isinstance(d, bool) or not isinstance(d, int) or d <= 0:
            raise InputError(
                f"variable {name!r}: shape dimensions must be positive ints",
                detail={"variable": name, "bad_dimension": d},
            )
        dims.append(d)
    return tuple(dims)
