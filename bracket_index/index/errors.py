"""Domain errors raised by the index engine and mapped to HTTP by the API."""

from __future__ import annotations

CATEGORY_STALE_VERSION = "STALE_VERSION"
CATEGORY_INVALID_RANGE = "INVALID_RANGE"
CATEGORY_NOT_A_BRACKET = "NOT_A_BRACKET"
CATEGORY_DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"


class IndexError_(Exception):
    """Base class for engine errors; carries a stable category string."""

    category = "INDEX_ERROR"


class DocumentNotFoundError(IndexError_):
    category = CATEGORY_DOCUMENT_NOT_FOUND

    def __init__(self, doc_id: int) -> None:
        super().__init__(f"document {doc_id} does not exist")
        self.doc_id = doc_id


class StaleVersionError(IndexError_):
    category = CATEGORY_STALE_VERSION

    def __init__(self, doc_id: int, expected: int, actual: int) -> None:
        super().__init__(
            f"document {doc_id}: edit based on version {expected} "
            f"but current version is {actual}; re-read and rebase the edit"
        )
        self.doc_id = doc_id
        self.expected = expected
        self.actual = actual


class InvalidRangeError(IndexError_):
    category = CATEGORY_INVALID_RANGE

    def __init__(self, start: int, end: int, length: int) -> None:
        super().__init__(
            f"edit range [{start}, {end}) is outside document length {length}"
        )
        self.start = start
        self.end = end
        self.length = length


class NotABracketError(IndexError_):
    category = CATEGORY_NOT_A_BRACKET

    def __init__(self, pos: int) -> None:
        super().__init__(f"position {pos} is not a structural bracket character")
        self.pos = pos
