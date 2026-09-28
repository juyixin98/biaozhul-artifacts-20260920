"""The :class:`Tensor` user-facing type: a layout view over shared storage."""
from __future__ import annotations

import itertools
from typing import Iterable, Sequence

import numpy as np
from numpy.lib.stride_tricks import as_strided

from ..config import SETTINGS
from . import layout
from .errors import (
    InvalidLayoutError,
    ReshapeCopyRequiredError,
)
from .storage import Storage

_tensor_counter = itertools.count(1)

# Exact self-overlap checks enumerate offsets only up to this size; larger
# tensors fall back to a structural check and flag the result as uncertain.
_EXACT_OVERLAP_LIMIT = 200_000


class Tensor:
    """A strided view ``(shape, strides, offset)`` over a float64 storage."""

    __slots__ = ("storage", "shape", "strides", "offset", "tensor_id", "name")

    def __init__(
        self,
        storage: Storage,
        shape: Sequence[int],
        strides: Sequence[int],
        offset: int = 0,
        *,
        name: str | None = None,
        tensor_id: int | None = None,
        validate: bool = True,
    ) -> None:
        shape = tuple(int(d) for d in shape)
        strides = tuple(int(s) for s in strides)
        if len(shape) != len(strides):
            raise InvalidLayoutError(
                f"ndim mismatch: shape {shape} vs strides {strides}"
            )
        if validate:
            layout.validate_bounds(shape, strides, int(offset), storage.size)
        self.storage = storage
        self.shape = shape
        self.strides = strides
        self.offset = int(offset)
        self.tensor_id = tensor_id if tensor_id is not None else next(_tensor_counter)
        self.name = name
        self.storage.register_view(self.tensor_id, shape, strides, self.offset)

    # ---------------------------------------------------------------- construction

    @classmethod
    def from_values(cls, values, *, name: str | None = None) -> "Tensor":
        """Create an owning C-contiguous tensor from array-like data."""
        arr = np.asarray(values, dtype=np.float64)
        storage = Storage.allocate(arr)
        return cls(storage, arr.shape, layout.c_strides(arr.shape), 0, name=name)

    @classmethod
    def zeros(cls, shape: Iterable[int], *, name: str | None = None) -> "Tensor":
        dims = layout.normalize_shape(shape, SETTINGS.max_elements)
        storage = Storage(np.zeros(layout.element_count(dims), dtype=np.float64))
        return cls(storage, dims, layout.c_strides(dims), 0, name=name)

    @classmethod
    def from_layout(
        cls,
        flat_data: Sequence[float],
        shape: Sequence[int],
        strides: Sequence[int],
        offset: int = 0,
        *,
        name: str | None = None,
    ) -> "Tensor":
        """Low-level constructor for arbitrary (possibly overlapping) views.

        ``flat_data`` is the raw storage buffer; the view is validated to stay
        inside it.  Useful for fixtures of hand-built stride patterns.
        """
        dims = layout.normalize_shape(shape, SETTINGS.max_elements)
        storage = Storage.allocate(np.asarray(flat_data, dtype=np.float64))
        return cls(storage, dims, strides, offset, name=name)

    def __del__(self) -> None:  # pragma: no cover - best-effort bookkeeping
        try:
            self.storage.unregister_view(self.tensor_id)
        except Exception:
            pass

    # ------------------------------------------------------------------ metadata

    @property
    def ndim(self) -> int:
        return len(self.shape)

    @property
    def size(self) -> int:
        return layout.element_count(self.shape)

    @property
    def storage_range(self) -> tuple[int, int]:
        return layout.address_bounds(self.shape, self.strides, self.offset)

    def is_c_contiguous(self) -> bool:
        return self.offset == 0 and layout.is_c_contiguous(self.shape, self.strides)

    def describe(self) -> dict:
        lo, hi = self.storage_range
        return {
            "tensor_id": self.tensor_id,
            "name": self.name,
            "storage_id": self.storage.storage_id,
            "shape": list(self.shape),
            "strides": list(self.strides),
            "offset": self.offset,
            "storage_interval": [lo, hi],
            "c_contiguous": self.is_c_contiguous(),
            "size": self.size,
        }

    # --------------------------------------------------------------- materialize

    def numpy_view(self) -> np.ndarray:
        """Strided numpy view sharing this tensor's storage (writes alias)."""
        # Shift the base pointer via slicing so the storage offset is honored;
        # as_strided then applies shape/strides relative to that base.
        base = self.storage.buffer[self.offset:]
        view = as_strided(
            base,
            shape=self.shape,
            strides=tuple(s * 8 for s in self.strides),
        )
        return view if self.shape else view.reshape(())

    def materialize(self) -> np.ndarray:
        """Return a fresh C-contiguous copy of the viewed values."""
        return np.array(self.numpy_view(), order="C", copy=True)

    def to_list(self) -> list:
        return self.materialize().tolist()

    def copy(self, *, name: str | None = None) -> "Tensor":
        """Deep copy: new storage, canonical C-contiguous layout."""
        return Tensor.from_values(self.materialize(), name=name)

    # --------------------------------------------------------------------- views

    def _view(
        self,
        shape: Sequence[int],
        strides: Sequence[int],
        offset: int,
        *,
        name: str | None = None,
    ) -> "Tensor":
        return Tensor(self.storage, shape, strides, offset, name=name)

    def transpose(self, axes: Sequence[int] | None = None, *, name: str | None = None) -> "Tensor":
        new_shape, new_strides = layout.transpose_layout(self.shape, self.strides, axes)
        return self._view(new_shape, new_strides, self.offset, name=name)

    @property
    def T(self) -> "Tensor":
        return self.transpose()

    def __getitem__(self, indices) -> "Tensor":
        if not isinstance(indices, tuple):
            indices = (indices,)
        py_indices = tuple(_py_index(i) for i in indices)
        new_shape, new_strides, new_offset = layout.apply_index(
            self.shape, self.strides, self.offset, py_indices
        )
        return self._view(new_shape, new_strides, new_offset)

    def reshape(
        self,
        new_shape: int | Sequence[int],
        *,
        allow_copy: bool = False,
        name: str | None = None,
    ) -> "Tensor":
        if isinstance(new_shape, int):
            new_shape = (new_shape,)
        new_shape = _resolve_infer_dim(self.shape, tuple(int(d) for d in new_shape))
        try:
            new_strides = layout.zero_copy_reshape(self.shape, self.strides, new_shape)
        except ReshapeCopyRequiredError:
            if not allow_copy:
                raise
            copied = self.copy()
            return copied._view(new_shape, layout.c_strides(new_shape), 0, name=name)
        return self._view(new_shape, new_strides, self.offset, name=name)

    def reshape_info(self, new_shape: int | Sequence[int]) -> dict:
        """Dry-run a reshape and report whether it copies, without doing it."""
        if isinstance(new_shape, int):
            new_shape = (new_shape,)
        new_shape = _resolve_infer_dim(self.shape, tuple(int(d) for d in new_shape))
        try:
            new_strides = layout.zero_copy_reshape(self.shape, self.strides, new_shape)
            return {
                "zero_copy": True,
                "new_shape": list(new_shape),
                "new_strides": list(new_strides),
            }
        except ReshapeCopyRequiredError as exc:
            return {
                "zero_copy": False,
                "new_shape": list(new_shape),
                "reason": str(exc),
            }

    def unsqueeze(self, axis: int) -> "Tensor":
        axis = axis + self.ndim + 1 if axis < 0 else axis
        new_shape = tuple(self.shape[:axis]) + (1,) + tuple(self.shape[axis:])
        new_strides = tuple(self.strides[:axis]) + (0,) + tuple(self.strides[axis:])
        return self._view(new_shape, new_strides, self.offset)

    def squeeze(self, axis: int | None = None) -> "Tensor":
        if axis is None:
            axes = [i for i, d in enumerate(self.shape) if d == 1]
        else:
            norm = axis + self.ndim if axis < 0 else axis
            if not 0 <= norm < self.ndim:
                from .errors import AxisError

                raise AxisError(f"axis {axis} out of range")
            if self.shape[norm] != 1:
                raise InvalidLayoutError(f"cannot squeeze axis {norm} of size {self.shape[norm]}")
            axes = [norm]
        drop = set(axes)
        new_shape = tuple(d for i, d in enumerate(self.shape) if i not in drop)
        new_strides = tuple(s for i, s in enumerate(self.strides) if i not in drop)
        return self._view(new_shape, new_strides, self.offset)

    # ------------------------------------------------------------------ aliasing

    def shares_storage(self, other: "Tensor") -> bool:
        return self.storage.storage_id == other.storage.storage_id

    def self_overlap(self) -> tuple[bool, bool]:
        """Return ``(overlaps, uncertain)``.

        ``overlaps`` means two distinct logical elements map to the same
        storage position.  ``uncertain`` means the exact check was too large
        to enumerate and only a structural test ran.
        """
        dims_gt1 = [i for i, d in enumerate(self.shape) if d > 1]
        if any(self.strides[i] == 0 for i in dims_gt1):
            return True, False
        if self.size <= _EXACT_OVERLAP_LIMIT:
            offsets = self._enumerate_offsets()
            return bool(np.unique(offsets).size != self.size), False
        # Structural fallback: repeated zero stride handled above; beyond the
        # exact budget, report uncertainty rather than a false negative.
        return False, True

    def _enumerate_offsets(self) -> np.ndarray:
        grids: list[np.ndarray] = []
        for d, s in zip(self.shape, self.strides):
            grids.append(np.arange(d, dtype=np.int64) * s)
        mesh = np.meshgrid(*grids, indexing="ij", copy=False) if grids else [np.zeros(1, dtype=np.int64)]
        offsets = self.offset
        for m in mesh:
            offsets = offsets + m
        return np.asarray(offsets, dtype=np.int64).reshape(-1)

    def write_fill(self, value: float) -> None:
        """Write a scalar through this tensor's (possibly repeated) positions."""
        self.numpy_view().fill(float(value))


def _py_index(index) -> slice | int:
    if isinstance(index, bool):
        raise InvalidLayoutError("boolean indexing is not supported; use slices/ints")
    if isinstance(index, slice):
        return index
    if isinstance(index, (int, np.integer)):
        return int(index)
    raise InvalidLayoutError(f"only slices and integers are supported, got {type(index)!r}")


def _resolve_infer_dim(old_shape: Sequence[int], new_shape: tuple[int, ...]) -> tuple[int, ...]:
    if sum(1 for d in new_shape if d == -1) > 1:
        raise InvalidLayoutError("only one dimension may be -1")
    if -1 in new_shape:
        total = layout.element_count(old_shape)
        known = layout.element_count(tuple(d for d in new_shape if d != -1))
        if known == 0 or total % known != 0:
            raise InvalidLayoutError(
                f"cannot infer -1 dimension for {tuple(old_shape)} -> {new_shape}"
            )
        new_shape = tuple(total // known if d == -1 else d for d in new_shape)
    if any(d < 0 for d in new_shape):
        raise InvalidLayoutError(f"invalid target shape {new_shape}")
    return layout.normalize_shape(new_shape, SETTINGS.max_elements)
