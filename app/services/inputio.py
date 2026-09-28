"""Input boundary: parsing, validation and synthetic sequence generation.

Nothing past this module trusts request data.  Numbers may arrive as JSON
numbers or as the explicit string tokens ``"NaN"``, ``"Infinity"`` and
``"-Infinity"`` (standard JSON has no NaN/Inf literal).
"""
from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np

from ..config import settings

_TOKEN_FLOAT = {
    "nan": math.nan,
    "infinity": math.inf,
    "+infinity": math.inf,
    "-infinity": -math.inf,
}


class InputValidationError(ValueError):
    """Input failed validation. ``code`` classifies the failure category."""

    def __init__(self, code: str, message: str, index: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.index = index

    def to_plain(self) -> dict[str, Any]:
        detail = {"code": self.code, "message": self.message}
        if self.index is not None:
            detail["index"] = self.index
        return detail


def parse_values(items: Iterable[Any]) -> np.ndarray:
    """Convert a request payload into a contiguous ``float64`` array."""
    if not isinstance(items, list):
        raise InputValidationError(
            "values_not_array", "field 'values' must be a JSON array of numbers"
        )
    n = len(items)
    if n == 0:
        raise InputValidationError("empty_input", "input sequence must contain at least one value")
    if n > settings.max_input_length:
        raise InputValidationError(
            "input_too_large",
            f"input length {n} exceeds configured maximum {settings.max_input_length}",
        )

    out = np.empty(n, dtype=np.float64)
    for i, item in enumerate(items):
        out[i] = _parse_scalar(item, i)
    return out


def _parse_scalar(item: Any, index: int) -> float:
    if isinstance(item, bool):  # bool is an int subclass; reject to avoid surprises
        raise InputValidationError("non_numeric", "boolean is not a valid summand", index)
    if isinstance(item, (int, float)):
        return float(item)
    if isinstance(item, str):
        token = item.strip().lower()
        if token in _TOKEN_FLOAT:
            return _TOKEN_FLOAT[token]
        raise InputValidationError(
            "unparseable_token",
            f"string {item!r} is not a number or one of NaN/Infinity/-Infinity",
            index,
        )
    raise InputValidationError("non_numeric", f"value of type {type(item).__name__} is not numeric", index)


def validate_block_size(block_size: int | None) -> int:
    size = settings.default_block_size if block_size is None else block_size
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise InputValidationError("bad_block_size", "block_size must be a positive integer")
    return size
