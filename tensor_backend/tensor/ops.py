"""Elementwise operations with broadcasting and overlap-safe writes.

Reads are cheap strided numpy views.  Writes distinguish two policies:

* ``raise``  – refuse an in-place write whose destination is self-overlapping
  or whose source and destination storage overlap (ambiguous semantics);
* ``temp``   – perform the read fully into a temporary contiguous buffer first,
  then write back, giving a deterministic result even with overlap.
"""
from __future__ import annotations

from enum import Enum
from typing import Callable

import numpy as np

from . import layout
from .errors import (
    BroadcastError,
    OverlappingWriteError,
)
from .tensor import Tensor

# Binary ufuncs exposed to the API; deliberately small and explicit.
_BINARY_UFUNCS: dict[str, Callable[[np.ndarray, np.ndarray], np.ndarray]] = {
    "add": np.add,
    "subtract": np.subtract,
    "multiply": np.multiply,
    "divide": np.true_divide,
    "maximum": np.maximum,
    "minimum": np.minimum,
}

_UNARY_UFUNCS: dict[str, Callable[[np.ndarray], np.ndarray]] = {
    "negate": np.negative,
    "abs": np.absolute,
    "exp": np.exp,
    "square": np.square,
}


class OverlapPolicy(str, Enum):
    RAISE = "raise"
    TEMP = "temp"


def list_binary_ops() -> list[str]:
    return sorted(_BINARY_UFUNCS)


def _c_copy(a: np.ndarray) -> np.ndarray:
    """Contiguous copy preserving 0-d shape (ascontiguousarray promotes 0-d to 1-d)."""
    return np.array(a, order="C", copy=True)


def list_unary_ops() -> list[str]:
    return sorted(_UNARY_UFUNCS)


def _broadcast_inputs(a: Tensor, b: Tensor) -> tuple[np.ndarray, np.ndarray, tuple[int, ...]]:
    target = layout.can_broadcast([a.shape, b.shape])
    sa = layout.broadcast_strides(a.shape, a.strides, target)
    sb = layout.broadcast_strides(b.shape, b.strides, target)
    va = _as_strided(a, target, sa)
    vb = _as_strided(b, target, sb)
    return va, vb, target


def _as_strided(t: Tensor, shape: tuple[int, ...], strides: tuple[int, ...]) -> np.ndarray:
    from numpy.lib.stride_tricks import as_strided

    return as_strided(
        t.storage.buffer[t.offset:],
        shape=shape,
        strides=tuple(s * 8 for s in strides),
    )


def binary(
    op: str,
    a: Tensor,
    b: Tensor,
    *,
    out: Tensor | None = None,
    overlap_policy: OverlapPolicy = OverlapPolicy.RAISE,
) -> Tensor:
    if op not in _BINARY_UFUNCS:
        raise KeyError(f"unknown binary op {op!r}; choose from {list_binary_ops()}")
    va, vb, target = _broadcast_inputs(a, b)

    if out is None:
        result = _BINARY_UFUNCS[op](va, vb)
        return Tensor.from_values(_c_copy(result))

    # In-place path: validate target and overlap before touching anything.
    if out.shape != target:
        raise BroadcastError(
            f"out shape {out.shape} does not match broadcast result {target}"
        )
    _check_write(out, [a, b], overlap_policy)
    result = _BINARY_UFUNCS[op](va, vb)
    if overlap_policy is OverlapPolicy.TEMP and (
        out.shares_storage(a) or out.shares_storage(b)
    ):
        # Fully evaluate before writing, so overlapping inputs stay valid.
        result = _c_copy(result)
    np.copyto(out.numpy_view(), result)
    return out


def unary(op: str, a: Tensor, *, out: Tensor | None = None) -> Tensor:
    if op not in _UNARY_UFUNCS:
        raise KeyError(f"unknown unary op {op!r}; choose from {list_unary_ops()}")
    if out is None:
        return Tensor.from_values(_c_copy(_UNARY_UFUNCS[op](a.numpy_view())))
    if out.shape != a.shape:
        raise BroadcastError(f"out shape {out.shape} does not match operand {a.shape}")
    _check_write(out, [a], OverlapPolicy.RAISE)
    np.copyto(out.numpy_view(), _UNARY_UFUNCS[op](a.numpy_view()))
    return out


def scale(a: Tensor, factor: float) -> Tensor:
    return Tensor.from_values(_c_copy(a.numpy_view() * float(factor)))


def assign(
    dst: Tensor,
    src: Tensor,
    *,
    overlap_policy: OverlapPolicy = OverlapPolicy.RAISE,
) -> Tensor:
    """Copy viewed values ``src -> dst`` (shapes must match exactly).

    Under ``temp`` the source is fully read into a contiguous temporary
    *before* any write, so an overlapping src/dst yields the same result as a
    copy through fresh memory.
    """
    if dst.shape != src.shape:
        raise BroadcastError(
            f"cannot assign shape {src.shape} into shape {dst.shape}"
        )
    _check_write(dst, [src], overlap_policy)
    rhs = src.numpy_view()
    if overlap_policy is OverlapPolicy.TEMP and dst.shares_storage(src):
        rhs = _c_copy(rhs)
    np.copyto(dst.numpy_view(), rhs)
    return dst


def reduce_sum(a: Tensor, axis: int | None = None) -> Tensor:
    """Reduction always materializes (strided read -> contiguous result)."""
    if axis is None:
        return Tensor.from_values(np.asarray(a.numpy_view().sum(), dtype=np.float64))
    norm = layout.normalize_axis(axis, a.ndim)
    return Tensor.from_values(_c_copy(a.numpy_view().sum(axis=norm)))


def matmul(a: Tensor, b: Tensor) -> Tensor:
    """Matrix product over strided views; result is freshly allocated."""
    if a.ndim < 1 or b.ndim < 1:
        raise BroadcastError("matmul requires at least 1-d operands")
    return Tensor.from_values(_c_copy(a.numpy_view() @ b.numpy_view()))


def _check_write(
    dst: Tensor,
    sources: list[Tensor],
    policy: OverlapPolicy,
) -> None:
    """Validate a write against a (possibly overlapping) destination.

    * A *self-overlapping* destination maps several logical elements to one
      storage position, so the value to write is itself undefined.  This is
      rejected under both policies — a temporary cannot invent a value.
    * Source/destination overlap is rejected under ``raise``; under ``temp``
      the caller is relied upon to fully evaluate the right-hand side into a
      contiguous temporary before writing (which the call sites do).
    """
    dst_overlaps, dst_uncertain = dst.self_overlap()
    if dst_overlaps:
        raise OverlappingWriteError(
            f"refusing write: destination tensor {dst.tensor_id} is self-overlapping "
            f"(distinct elements share storage positions); write into a fresh "
            f"non-overlapping tensor instead"
        )
    if dst_uncertain and policy is OverlapPolicy.RAISE:
        raise OverlappingWriteError(
            f"refusing write: destination tensor {dst.tensor_id} is too large to "
            f"prove non-self-overlap exactly; use overlap_policy='temp'"
        )

    dlo, dhi = dst.storage_range
    for s in sources:
        if not s.shares_storage(dst):
            continue
        if _same_layout(s, dst):
            # Identical 1:1 mapping: an elementwise read-then-write is
            # unambiguous (this is ordinary in-place unary/binary update).
            continue
        slo, shi = s.storage_range
        if dlo <= shi and slo <= dhi and policy is OverlapPolicy.RAISE:
            raise OverlappingWriteError(
                f"refusing write: destination interval [{dlo},{dhi}] overlaps "
                f"source interval [{slo},{shi}] in storage {dst.storage.storage_id}; "
                f"use overlap_policy='temp' for a deterministic temporary-copy result"
            )


def _same_layout(a: Tensor, b: Tensor) -> bool:
    return (
        a.storage.storage_id == b.storage.storage_id
        and a.offset == b.offset
        and a.shape == b.shape
        and a.strides == b.strides
    )
