"""Explicit, closed set of failure categories.

The service never collapses an exception or an unknown state into a success
response. Every error the core can raise is a distinct :class:`SparseUpdateError`
subclass with a stable ``code`` so that callers and tests can assert the exact
failure category.
"""

from __future__ import annotations


class SparseUpdateError(Exception):
    """Base class for all domain errors. ``code`` is stable over releases."""

    code = "sparse_update_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationBatchRejectedError(SparseUpdateError):
    """The *whole* batch was rejected before any state was touched.

    Raised for malformed shape/dtype, mismatched lengths, or any out-of-range
    index. Indices are validated against the full table up front, so a single
    bad index rejects the entire batch.
    """

    code = "batch_rejected"


class EmptyBatchError(SparseUpdateError):
    """A batch carried no rows (empty index array).

    This is a well-defined, explicit outcome (a no-op), not a silent success:
    no aggregation, no clipping and no optimizer step happen, and the training
    step counter is not advanced.
    """

    code = "empty_batch"


class PersistenceError(SparseUpdateError):
    """A checkpoint write/load failed. The transaction did not commit."""

    code = "persistence_error"


class StateShapeError(SparseUpdateError):
    """A loaded checkpoint is incompatible with the configured table."""

    code = "state_shape_error"
