"""Tensor value type and memory accounting.

Tensors are thin wrappers over ``numpy.ndarray``.  The accounting model used
throughout the project counts **float elements**, never bytes: every fixture
uses the same dtype so element counts are directly comparable, and tests can
assert exact integer memory numbers without platform dependence.

A tensor also remembers the memory "cost" of producing it so the planner can
score workspace usage: :class:`TensorDesc` is the symbolic counterpart used by
the planner (no allocated data), :class:`TensorValue` carries real data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Tuple

import numpy as np

from .errors import InvalidInputError

SUPPORTED_DTYPE = np.float64
SUPPORTED_DTYPE_NAME = "float64"
_ELEM_NBYTES = 8  # float64


@dataclass(frozen=True)
class TensorDesc:
    """Symbolic description of a tensor (shape + memory cost in elements)."""

    shape: Tuple[int, ...]
    name: str = ""

    @property
    def numel(self) -> int:
        n = 1
        for d in self.shape:
            n *= d
        return n

    @property
    def nbytes(self) -> int:
        return self.numel * _ELEM_NBYTES

    def __post_init__(self) -> None:
        if not self.shape:
            raise InvalidInputError(
                "tensor shape must have at least one dimension",
                code="E_TENSOR_SHAPE_EMPTY",
                context={"name": self.name},
            )
        for d in self.shape:
            if not isinstance(d, int) or isinstance(d, bool) or d <= 0:
                raise InvalidInputError(
                    "tensor dimensions must be positive integers",
                    code="E_TENSOR_SHAPE_INVALID",
                    context={"name": self.name, "shape": list(self.shape)},
                )


@dataclass
class TensorValue:
    """A concrete tensor: ``numpy.ndarray`` plus provenance metadata."""

    data: np.ndarray
    name: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.data, np.ndarray):
            raise InvalidInputError(
                "TensorValue requires a numpy.ndarray",
                code="E_TENSOR_NOT_NDARRAY",
                context={"name": self.name, "type": type(self.data).__name__},
            )
        if self.data.dtype != SUPPORTED_DTYPE:
            self.data = self.data.astype(SUPPORTED_DTYPE, copy=False)
        if not np.all(np.isfinite(self.data)):
            raise InvalidInputError(
                "tensor contains NaN or Inf",
                code="E_TENSOR_NONFINITE",
                context={"name": self.name},
            )

    @property
    def shape(self) -> Tuple[int, ...]:
        return tuple(self.data.shape)

    @property
    def numel(self) -> int:
        return int(self.data.size)

    @property
    def nbytes(self) -> int:
        return self.data.nbytes

    def desc(self) -> TensorDesc:
        return TensorDesc(shape=self.shape, name=self.name)


def coerce_array(value: Any, name: str = "") -> np.ndarray:
    """Validate an incoming JSON-ish value and return a finite float64 array."""

    if not isinstance(value, (list, tuple, int, float)) and not (
        isinstance(value, np.ndarray)
    ):
        raise InvalidInputError(
            "expected a nested list of numbers",
            code="E_TENSOR_NOT_ARRAY",
            context={"name": name, "type": type(value).__name__},
        )
    arr = np.asarray(value, dtype=SUPPORTED_DTYPE)
    if arr.ndim == 0:
        raise InvalidInputError(
            "scalar tensors are not supported",
            code="E_TENSOR_SCALAR",
            context={"name": name},
        )
    if not np.all(np.isfinite(arr)):
        raise InvalidInputError(
            "tensor contains NaN or Inf",
            code="E_TENSOR_NONFINITE",
            context={"name": name},
        )
    return arr


def shapes_match(a: TensorDesc, b: TensorDesc) -> bool:
    return a.shape == b.shape
