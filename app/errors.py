"""Typed failure categories shared by the API and the logs.

Every user-visible failure carries one of these categories so that
clients and reviewers can distinguish *why* something failed instead of
only seeing that it failed.
"""

from __future__ import annotations

from enum import Enum


class FailureCategory(str, Enum):
    INVALID_BASE64 = "INVALID_BASE64"
    EMPTY_DOCUMENT = "EMPTY_DOCUMENT"
    EMPTY_DOCUMENT_ID = "EMPTY_DOCUMENT_ID"
    DUPLICATE_DOC_ID = "DUPLICATE_DOC_ID"
    DOCUMENT_TOO_LARGE = "DOCUMENT_TOO_LARGE"
    TOO_MANY_DOCUMENTS = "TOO_MANY_DOCUMENTS"
    CORPUS_TOO_LARGE = "CORPUS_TOO_LARGE"
    CORPUS_NOT_FOUND = "CORPUS_NOT_FOUND"
    INVALID_MIN_DOCS = "INVALID_MIN_DOCS"
    INVALID_MAX_CANDIDATES = "INVALID_MAX_CANDIDATES"
    EMPTY_QUERY_BATCH = "EMPTY_QUERY_BATCH"
    INDEX_CORRUPT = "INDEX_CORRUPT"
    INTERNAL = "INTERNAL"


class AppError(Exception):
    """Domain error carrying a stable failure category."""

    def __init__(self, category: FailureCategory, detail: str) -> None:
        super().__init__(f"{category.value}: {detail}")
        self.category = category
        self.detail = detail
