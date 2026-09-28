"""Error taxonomy for the tensor backend.

Every failure the backend can produce has a distinct category so that API
responses, logs and tests can assert *why* an operation failed instead of
catching a generic exception.
"""
from __future__ import annotations


class TensorError(RuntimeError):
    """Base class for all tensor-backend errors."""

    category = "tensor_error"


class ShapeOverflowError(TensorError):
    """Raised when a shape product / offset arithmetic exceeds addressable size."""

    category = "shape_overflow"


class OutOfBoundsError(TensorError):
    """Raised when an index or view extent reaches outside the storage buffer."""

    category = "out_of_bounds"


class InvalidLayoutError(TensorError):
    """Raised for shapes/strides/offset triples that cannot describe a view."""

    category = "invalid_layout"


class InvalidStrideError(InvalidLayoutError):
    """Raised when a stride constraint is violated (e.g. step zero in a slice)."""

    category = "invalid_stride"


class AxisError(TensorError):
    """Raised when an axis index is out of range or a permutation is invalid."""

    category = "axis_error"


class BroadcastError(TensorError):
    """Raised when shapes cannot be broadcast together."""

    category = "broadcast_error"


class ReshapeCopyRequiredError(TensorError):
    """Raised when a reshape cannot be served as a zero-copy view.

    The attached message explains which stride pattern forces the copy so
    callers can make an explicit decision instead of NumPy copying silently.
    """

    category = "reshape_copy_required"


class OverlappingWriteError(TensorError):
    """Raised under the ``raise`` overlap policy when source and destination
    storage overlap and an in-place write would be ambiguous."""

    category = "overlapping_write"


class DTypeError(TensorError):
    """Raised for unsupported dtypes."""

    category = "dtype_error"
