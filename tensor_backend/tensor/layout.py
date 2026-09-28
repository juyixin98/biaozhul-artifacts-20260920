"""Pure layout mathematics: shape, strides and storage offset.

Nothing in this module touches NumPy storage.  A layout is the triple
``(shape, strides, offset)`` measured in *elements* (byte stride = element
stride * itemsize, and the backend only supports a fixed itemsize of 8 for
float64).

Negative strides are supported everywhere; the legal range for every view is
verified against the owning buffer before access.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from .errors import (
    AxisError,
    BroadcastError,
    InvalidLayoutError,
    InvalidStrideError,
    OutOfBoundsError,
    ReshapeCopyRequiredError,
    ShapeOverflowError,
)

# Addresses are validated against this cap *before* any allocation or access,
# mirroring the size-multiplication overflow check a native runtime performs.
ADDRESS_CAP = 2 ** 63 - 1


def _checked_mul(a: int, b: int) -> int:
    result = a * b
    if result > ADDRESS_CAP:
        raise ShapeOverflowError(
            f"size multiplication overflow: {a} * {b} exceeds addressable cap "
            f"{ADDRESS_CAP}"
        )
    return result


def normalize_shape(shape: Iterable[int], max_elements: int) -> tuple[int, ...]:
    dims = tuple(int(d) for d in shape)
    if any(d < 0 for d in dims):
        raise InvalidLayoutError(f"shape dimensions must be non-negative, got {dims}")
    total = 1
    for d in dims:
        if d == 0:
            total = 0
            break
        total = _checked_mul(total, d)
    if total > max_elements:
        raise ShapeOverflowError(
            f"tensor of {total} elements exceeds configured limit {max_elements}"
        )
    return dims


def element_count(shape: Sequence[int]) -> int:
    total = 1
    for d in shape:
        total = _checked_mul(total, d)
        if total == 0:
            return 0
    return total


def c_strides(shape: Sequence[int]) -> tuple[int, ...]:
    """Row-major (C-order) element strides; zero dims contribute factor 1."""
    strides: list[int] = []
    running = 1
    for d in reversed(shape):
        strides.append(running)
        running = _checked_mul(running, d if d != 0 else 1)
    return tuple(reversed(strides))


def f_strides(shape: Sequence[int]) -> tuple[int, ...]:
    strides: list[int] = []
    running = 1
    for d in shape:
        strides.append(running)
        running = _checked_mul(running, d if d != 0 else 1)
    return tuple(strides)


def is_c_contiguous(shape: Sequence[int], strides: Sequence[int]) -> bool:
    """NumPy-style C-contiguity: walk innermost axes outward, skipping size-1."""
    expected = 1
    for i in range(len(shape) - 1, -1, -1):
        d = shape[i]
        if d != 1:
            if strides[i] != expected:
                return False
            expected *= d if d != 0 else 1
    return True


def is_f_contiguous(shape: Sequence[int], strides: Sequence[int]) -> bool:
    expected = 1
    for i in range(len(shape)):
        d = shape[i]
        if d != 1:
            if strides[i] != expected:
                return False
            expected *= d if d != 0 else 1
    return True


def normalize_axis(axis: int, ndim: int) -> int:
    if ndim == 0:
        raise AxisError(f"no axes exist on a 0-d tensor, got axis={axis}")
    if not -ndim <= axis < ndim:
        raise AxisError(f"axis {axis} is out of bounds for tensor with {ndim} dimensions")
    return axis + ndim if axis < 0 else axis


def normalize_axes(axes: Iterable[int], ndim: int) -> tuple[int, ...]:
    out = tuple(normalize_axis(int(a), ndim) for a in axes)
    if len(out) != ndim or sorted(out) != list(range(ndim)):
        raise AxisError(
            f"axes must be a permutation of 0..{ndim - 1}, got {tuple(axes)}"
        )
    return out


def address_bounds(
    shape: Sequence[int],
    strides: Sequence[int],
    offset: int,
) -> tuple[int, int]:
    """Return ``(min_index, max_index)`` reachable in the storage buffer.

    Works for positive, zero and negative strides and empty tensors (the range
    collapses to the single offset point when no elements exist).
    """
    lo = hi = offset
    if element_count(shape) == 0:
        return lo, hi
    for d, s in zip(shape, strides):
        if d == 1 or s == 0:
            continue
        span = (d - 1) * s  # negative for negative strides
        lo += min(0, span)
        hi += max(0, span)
    return lo, hi


def validate_bounds(
    shape: Sequence[int],
    strides: Sequence[int],
    offset: int,
    storage_elements: int,
) -> None:
    if offset < 0:
        raise OutOfBoundsError(f"negative storage offset {offset}")
    if len(shape) != len(strides):
        raise InvalidLayoutError(
            f"shape ndim {len(shape)} != strides ndim {len(strides)}"
        )
    # An empty view never reads any storage position, so it cannot be out of
    # bounds (the offset is still required to be non-negative above).
    if element_count(shape) == 0:
        return
    lo, hi = address_bounds(shape, strides, offset)
    if hi >= storage_elements or lo < 0:
        raise OutOfBoundsError(
            f"view reaches storage indices [{lo}, {hi}] but buffer holds "
            f"{storage_elements} elements (offset={offset}, shape={tuple(shape)}, "
            f"strides={tuple(strides)})"
        )


@dataclass(frozen=True)
class SlicedAxis:
    start: int
    step: int
    length: int


def normalize_slice(index: slice | int, length: int) -> SlicedAxis | tuple[int, int]:
    """Normalize one axis against its dimension.

    Returns :class:`SlicedAxis` for a slice, or ``(integer_position, 0)``
    marker for an integer index.
    """
    if isinstance(index, bool):
        raise InvalidLayoutError("boolean indexing is not supported; use slices/ints")
    if isinstance(index, int):
        idx = index + length if index < 0 else index
        if not 0 <= idx < length:
            raise OutOfBoundsError(
                f"integer index {index} out of bounds for axis of size {length}"
            )
        return (idx, 0)

    step = 1 if index.step is None else int(index.step)
    if step == 0:
        raise InvalidStrideError("slice step cannot be zero")
    start = index.start
    stop = index.stop
    if start is None:
        start = 0 if step > 0 else length - 1
    if stop is None:
        stop = length if step > 0 else -length - 1
    start = int(start)
    stop = int(stop)
    if start < 0:
        start += length
    if stop < 0:
        stop += length
    if step > 0:
        start = max(0, min(start, length))
        stop = max(0, min(stop, length))
        n = max(0, math.ceil((stop - start) / step))
    else:
        start = max(-1, min(start, length - 1))
        stop = max(-1, min(stop, length - 1))
        n = max(0, math.ceil((stop - start) / step))
    return SlicedAxis(start=start, step=step, length=n)


def apply_index(
    shape: Sequence[int],
    strides: Sequence[int],
    offset: int,
    indices: Sequence[slice | int],
) -> tuple[tuple[int, ...], tuple[int, ...], int]:
    """Apply per-axis basic indexing (slices and integers), NumPy semantics."""
    if len(indices) > len(shape):
        raise AxisError(
            f"too many indices: tensor has {len(shape)} dims, got {len(indices)}"
        )
    new_shape: list[int] = []
    new_strides: list[int] = []
    new_offset = offset
    for axis, index in enumerate(indices):
        norm = normalize_slice(index, shape[axis])
        if isinstance(norm, tuple):  # integer index: axis collapses
            new_offset += norm[0] * strides[axis]
            continue
        new_offset += norm.start * strides[axis]
        new_shape.append(norm.length)
        new_strides.append(norm.step * strides[axis])
    # Un-indexed trailing axes remain untouched (NumPy basic-indexing rule).
    for axis in range(len(indices), len(shape)):
        new_shape.append(shape[axis])
        new_strides.append(strides[axis])
    return tuple(new_shape), tuple(new_strides), new_offset


def transpose_layout(
    shape: Sequence[int],
    strides: Sequence[int],
    axes: Sequence[int] | None,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    ndim = len(shape)
    if axes is None:
        perm = tuple(range(ndim))[::-1]
    else:
        perm = normalize_axes(axes, ndim)
    return tuple(shape[i] for i in perm), tuple(strides[i] for i in perm)


def can_broadcast(shapes: Iterable[Sequence[int]]) -> tuple[int, ...]:
    shape_list = [tuple(s) for s in shapes]
    ndim = max((len(s) for s in shape_list), default=0)
    target = [1] * ndim
    for shape in shape_list:
        pad = ndim - len(shape)
        for i, d in enumerate(shape):
            j = i + pad
            if d != 1 and target[j] != 1 and d != target[j]:
                raise BroadcastError(
                    f"shapes {shape_list} cannot be broadcast together"
                )
            if d != 1:
                target[j] = d
    return tuple(target)


def broadcast_strides(
    shape: Sequence[int],
    strides: Sequence[int],
    target_shape: Sequence[int],
) -> tuple[int, ...]:
    """Strides for broadcasting ``shape`` up to ``target_shape`` (right-aligned).

    Newly prepended or stretched dimensions receive stride ``0`` — the
    contract's "broadcast zero stride".
    """
    pad = len(target_shape) - len(shape)
    if pad < 0:
        raise InvalidLayoutError(
            f"cannot broadcast shape {tuple(shape)} to {tuple(target_shape)}"
        )
    out = [0] * pad
    for d, s, td in zip(shape, strides, target_shape[pad:]):
        if d == td:
            out.append(s)
        elif d == 1:
            out.append(0)
        else:
            raise BroadcastError(
                f"shape {tuple(shape)} cannot broadcast to {tuple(target_shape)}: "
                f"dimension {d} != {td}"
            )
    return tuple(out)




def _layout_index_at_flat(
    k: int,
    shape: Sequence[int],
    strides: Sequence[int],
    offset: int,
) -> int:
    """Storage index reached by the k-th element in C-order traversal."""
    idx = offset
    for d, s in zip(reversed(shape), reversed(strides)):
        idx += (k % d) * s
        k //= d
    return idx


def _reshape_identity_holds(
    old_shape: Sequence[int],
    old_strides: Sequence[int],
    offset: int,
    new_shape: Sequence[int],
    new_strides: Sequence[int],
) -> bool:
    """Prove old/new indexing maps coincide at every element.

    Both maps are piecewise-affine functions of the C-order flat index ``k``.
    Within each interval between consecutive row boundaries the storage index
    equals ``slope*k + intercept``; therefore checking ``k=0`` and every
    boundary neighbour ``c-1, c`` proves equality on the whole range.  This is
    O(ndim^2) probes rather than a per-element scan.
    """
    n = element_count(old_shape)
    if n <= 1:
        return True
    candidates = {0, n - 1}
    for dims in (old_shape, new_shape):
        boundary = 1
        for d in reversed(dims):
            boundary *= d
            k = boundary
            while k < n:
                candidates.add(k - 1)
                candidates.add(k)
                k += boundary
    for k in candidates:
        if _layout_index_at_flat(k, old_shape, old_strides, offset) != _layout_index_at_flat(
            k, new_shape, new_strides, offset
        ):
            return False
    return True



def zero_copy_reshape(
    shape: Sequence[int],
    strides: Sequence[int],
    new_shape: Sequence[int],
) -> tuple[int, ...]:
    """Return strides for a zero-copy reshape; raise if a copy is mandatory.

    Criterion (exact, not a heuristic): let ``f(k)`` be the storage index of
    the k-th element in C-order traversal of the *old* layout.  If a view with
    the new shape exists, the stride of new axis ``i`` is uniquely forced:

        new_stride[i] = f(B_i) - f(0),      B_i = prod(new_shape[i+1:])

    (the storage distance between adjacent rows of that axis).  The forced
    strides are accepted iff the induced new map equals ``f`` at ``k = 0`` and
    every row boundary ``c-1, c`` of both layouts — the two piecewise-affine
    maps then coincide on the whole range.
    """
    new_shape = tuple(new_shape)
    old_n = element_count(shape)
    new_n = element_count(new_shape)
    if old_n != new_n:
        raise InvalidLayoutError(
            f"cannot reshape {tuple(shape)} ({old_n} elements) into "
            f"{tuple(new_shape)} ({new_n} elements)"
        )
    if old_n <= 1:
        # Scalars / empties alias nothing; use canonical C-order strides.
        return c_strides(new_shape)

    f0 = _layout_index_at_flat(0, shape, strides, 0)
    forced: list[int] = []
    boundary = 1
    for d in reversed(new_shape):
        if boundary < old_n:
            forced.append(_layout_index_at_flat(boundary, shape, strides, 0) - f0)
        else:
            # Leading size-1 axes never move the index; the stride value is
            # unconstrained (address_bounds ignores size-1 dims).
            forced.append(0)
        boundary *= d if d != 0 else 1
    candidate = tuple(reversed(forced))

    if _reshape_identity_holds(shape, strides, 0, new_shape, candidate):
        return candidate
    raise ReshapeCopyRequiredError(
        f"reshape {tuple(shape)} -> {tuple(new_shape)} requires a copy: "
        f"C-order traversal of the source is not contiguous under the target "
        f"shape (forced strides would be {candidate})"
    )
