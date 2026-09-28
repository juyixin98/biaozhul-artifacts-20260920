"""Query/parameter validation at the HTTP boundary.

Schema-level validation lives in :mod:`entity_resolution.models`; this module
validates *query-string* parameters and cross-field request rules that are
awkward to express in pydantic, returning :class:`InvalidRequestError` so the
API emits the same categorical envelope as every other failure.
"""

from __future__ import annotations

from .errors import InvalidRequestError

MIN_THRESHOLD = 0.0
MAX_THRESHOLD = 1.0


def parse_threshold(raw: str | None, default: float) -> float:
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidRequestError(
            "threshold must be a number in [0, 1]",
            {"parameter": "threshold", "received": raw},
        ) from exc
    if not MIN_THRESHOLD <= value <= MAX_THRESHOLD:
        raise InvalidRequestError(
            "threshold must be within [0, 1]",
            {"parameter": "threshold", "received": value},
        )
    return value


def parse_run_id(raw: str | None) -> str | None:
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned or len(cleaned) > 200 or any(c.isspace() for c in cleaned):
        raise InvalidRequestError(
            "run_id must be a non-empty token without whitespace",
            {"parameter": "run_id", "received": raw},
        )
    return cleaned
