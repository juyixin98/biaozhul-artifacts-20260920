"""Tensor value objects and boundary validation.

This module owns *what a tensor is* in the runtime: a name, a shape, a
dtype, and the checks every value must pass before it may cross a process
or trust boundary.  It deliberately knows nothing about training rounds,
buckets, or networking.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import numpy as np

#: dtypes the runtime is willing to reduce.  Float64 is the default so the
#: teaching runtime stays bit-comparable against the reference oracle.
SUPPORTED_DTYPES: Tuple[str, ...] = ("float64", "float32")


class TensorSpecError(ValueError):
    """Raised when a tensor spec or value violates the runtime contract."""


@dataclass(frozen=True)
class TensorSpec:
    """Immutable description of one named tensor (parameter or gradient)."""

    name: str
    shape: Tuple[int, ...]
    dtype: str = "float64"

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise TensorSpecError("tensor name must be a non-empty string")
        if not self.shape:
            raise TensorSpecError(
                f"tensor {self.name!r}: shape must have at least one dimension"
            )
        for dim in self.shape:
            if not isinstance(dim, int) or dim <= 0:
                raise TensorSpecError(
                    f"tensor {self.name!r}: dimensions must be positive ints, "
                    f"got {self.shape!r}"
                )
        if self.dtype not in SUPPORTED_DTYPES:
            raise TensorSpecError(
                f"tensor {self.name!r}: unsupported dtype {self.dtype!r}; "
                f"supported: {SUPPORTED_DTYPES}"
            )

    @property
    def size(self) -> int:
        """Number of scalar elements."""
        return int(np.prod(self.shape, dtype=np.int64))

    @property
    def np_dtype(self) -> np.dtype:
        return np.dtype(self.dtype)

    def zeros(self) -> np.ndarray:
        """A zero array conforming to this spec (explicit placeholder value)."""
        return np.zeros(self.shape, dtype=self.np_dtype)

    def validate_value(self, value: np.ndarray, *, require_finite: bool = True) -> None:
        """Check ``value`` against this spec; raise :class:`TensorSpecError` if not.

        This is the single place where shape/dtype/finiteness are enforced, so
        that "looks fine on normal input, silently wrong on edge input" cannot
        happen downstream.
        """
        arr = np.asarray(value)
        if arr.shape != self.shape:
            raise TensorSpecError(
                f"tensor {self.name!r}: shape mismatch, expected {self.shape}, "
                f"got {tuple(arr.shape)}"
            )
        if arr.dtype != self.np_dtype:
            raise TensorSpecError(
                f"tensor {self.name!r}: dtype mismatch, expected {self.dtype}, "
                f"got {arr.dtype}"
            )
        if require_finite and not bool(np.all(np.isfinite(arr))):
            raise TensorSpecError(
                f"tensor {self.name!r}: contains NaN or Inf; refusing to "
                f"propagate non-finite values"
            )
