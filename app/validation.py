"""Query validation, isolated from the HTTP layer so it is unit-testable."""

from __future__ import annotations

from app.config import Settings
from app.errors import AppError, FailureCategory


def validate_min_docs(min_docs: int, doc_count: int) -> None:
    """min_docs selects coverage in DISTINCT documents, not occurrences."""
    if min_docs < 1 or min_docs > doc_count:
        raise AppError(
            FailureCategory.INVALID_MIN_DOCS,
            f"min_docs={min_docs} is outside [1, {doc_count}] for this corpus",
        )


def resolve_max_candidates(requested: int | None, settings: Settings) -> int:
    if requested is None:
        return settings.default_max_candidates
    if requested < 1 or requested > settings.max_candidates_cap:
        raise AppError(
            FailureCategory.INVALID_MAX_CANDIDATES,
            f"max_candidates={requested} is outside [1, {settings.max_candidates_cap}]",
        )
    return requested
