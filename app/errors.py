"""Typed error hierarchy.

Errors are categorized so that the API (and tests) can assert on a specific
failure *category* rather than matching message text. Unknown states are
never folded into success: every kernel failure maps to one of the codes
below.
"""
from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    INVALID_SHAPE = "INVALID_SHAPE"
    INVALID_INDICES = "INVALID_INDICES"
    DUPLICATE_ENTRIES = "DUPLICATE_ENTRIES"
    NOT_SQUARE = "NOT_SQUARE"
    ASYMMETRIC = "ASYMMETRIC"
    EMPTY_MATRIX = "EMPTY_MATRIX"
    TOO_LARGE = "TOO_LARGE"
    SIZE_MISMATCH = "SIZE_MISMATCH"
    NON_SPD_PIVOT = "NON_SPD_PIVOT"          # numerical failure during factor
    SINGULAR_PIVOT = "SINGULAR_PIVOT"
    PATTERN_MISMATCH = "PATTERN_MISMATCH"    # reuse rejected
    UNSUPPORTED = "UNSUPPORTED"
    INTERNAL = "INTERNAL"


class SparseSpdError(Exception):
    """Base class carrying a machine-readable error code."""

    code: ErrorCode = ErrorCode.INTERNAL

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class InputValidationError(SparseSpdError):
    def __init__(self, code: ErrorCode, message: str,
                 *, details: dict | None = None) -> None:
        super().__init__(message, details=details)
        self.code = code


class FactorizationError(SparseSpdError):
    """Numerical failure. ``pivot`` is the *permuted* elimination index;
    ``original_index`` maps it back to the caller's ordering."""

    def __init__(self, code: ErrorCode, message: str, *,
                 pivot: int | None = None,
                 original_index: int | None = None,
                 pivot_value: float | None = None,
                 details: dict | None = None) -> None:
        merged = dict(details or {})
        if pivot is not None:
            merged["pivot"] = pivot
        if original_index is not None:
            merged["original_index"] = original_index
        if pivot_value is not None:
            merged["pivot_value"] = pivot_value
        super().__init__(message, details=merged)
        self.code = code
        self.pivot = pivot
        self.original_index = original_index
        self.pivot_value = pivot_value


class PatternMismatchError(SparseSpdError):
    code = ErrorCode.PATTERN_MISMATCH


__all__ = [
    "ErrorCode",
    "SparseSpdError",
    "InputValidationError",
    "FactorizationError",
    "PatternMismatchError",
]
