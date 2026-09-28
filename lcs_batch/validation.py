"""Query and payload validation with typed failure categories.

Every rejected input raises :class:`LcsError` carrying a stable
:class:`ErrorCategory` so API responses, logs and tests can assert on the
failure class rather than on message text.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from enum import Enum

from .config import Settings


class ErrorCategory(str, Enum):
    # Corpus / payload validation
    EMPTY_CORPUS = "EMPTY_CORPUS"
    TOO_MANY_DOCUMENTS = "TOO_MANY_DOCUMENTS"
    DUPLICATE_DOC_ID = "DUPLICATE_DOC_ID"
    INVALID_DOC_ID = "INVALID_DOC_ID"
    EMPTY_DOCUMENT = "EMPTY_DOCUMENT"
    DOCUMENT_TOO_LARGE = "DOCUMENT_TOO_LARGE"
    CORPUS_TOO_LARGE = "CORPUS_TOO_LARGE"
    INVALID_CONTENT_ENCODING = "INVALID_CONTENT_ENCODING"
    # Query validation
    INVALID_QUERY_ID = "INVALID_QUERY_ID"
    INVALID_MIN_DOCS = "INVALID_MIN_DOCS"
    MIN_DOCS_EXCEEDS_CORPUS = "MIN_DOCS_EXCEEDS_CORPUS"
    INVALID_MAX_CANDIDATES = "INVALID_MAX_CANDIDATES"
    EMPTY_BATCH = "EMPTY_BATCH"
    # Index state
    INDEX_NOT_FOUND = "INDEX_NOT_FOUND"
    INDEX_CORRUPT = "INDEX_CORRUPT"
    KERNEL_VERSION_MISMATCH = "KERNEL_VERSION_MISMATCH"


class LcsError(Exception):
    """Domain error with a stable machine-readable category."""

    def __init__(self, category: ErrorCategory, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message

    def to_dict(self) -> dict:
        return {"category": self.category.value, "message": self.message}


_DOC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MIN_DOCS_FLOOR = 2  # "common" substring semantics require sharing across >= 2 docs


def validate_document_payloads(
    raw_documents: object, settings: Settings
) -> list[tuple[str, bytes]]:
    """Validate the raw JSON payload of a build request.

    Returns a list of ``(doc_id, content)`` pairs in submission order.
    """
    if not isinstance(raw_documents, list) or not raw_documents:
        raise LcsError(ErrorCategory.EMPTY_CORPUS, "documents must be a non-empty list")
    if len(raw_documents) > settings.max_documents:
        raise LcsError(
            ErrorCategory.TOO_MANY_DOCUMENTS,
            f"document count {len(raw_documents)} exceeds limit {settings.max_documents}",
        )

    parsed: list[tuple[str, bytes]] = []
    seen_ids: set[str] = set()
    total_bytes = 0
    for position, item in enumerate(raw_documents):
        if not isinstance(item, dict):
            raise LcsError(
                ErrorCategory.INVALID_DOC_ID,
                f"document at position {position} is not an object",
            )
        doc_id = item.get("doc_id")
        if not isinstance(doc_id, str) or not _DOC_ID_RE.match(doc_id):
            raise LcsError(
                ErrorCategory.INVALID_DOC_ID,
                f"doc_id at position {position} must match {_DOC_ID_RE.pattern}",
            )
        if doc_id in seen_ids:
            raise LcsError(
                ErrorCategory.DUPLICATE_DOC_ID, f"duplicate doc_id: {doc_id!r}"
            )
        seen_ids.add(doc_id)

        content_b64 = item.get("content_b64")
        if not isinstance(content_b64, str):
            raise LcsError(
                ErrorCategory.INVALID_CONTENT_ENCODING,
                f"document {doc_id!r}: content_b64 must be a base64 string",
            )
        try:
            content = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise LcsError(
                ErrorCategory.INVALID_CONTENT_ENCODING,
                f"document {doc_id!r}: content_b64 is not valid base64",
            ) from exc
        if not content:
            raise LcsError(
                ErrorCategory.EMPTY_DOCUMENT, f"document {doc_id!r} is empty"
            )
        if len(content) > settings.max_document_bytes:
            raise LcsError(
                ErrorCategory.DOCUMENT_TOO_LARGE,
                f"document {doc_id!r} has {len(content)} bytes, "
                f"limit is {settings.max_document_bytes}",
            )
        total_bytes += len(content)
        parsed.append((doc_id, content))

    # +1 separator symbol per document
    if total_bytes + len(parsed) > settings.max_total_symbols:
        raise LcsError(
            ErrorCategory.CORPUS_TOO_LARGE,
            f"encoded corpus needs {total_bytes + len(parsed)} symbols, "
            f"limit is {settings.max_total_symbols}",
        )
    return parsed


@dataclass(frozen=True)
class QuerySpec:
    query_id: str
    min_docs: int
    max_candidates: int


def validate_query_spec(raw: object, doc_count: int, settings: Settings) -> QuerySpec:
    """Validate one raw query object against the indexed corpus size."""
    if not isinstance(raw, dict):
        raise LcsError(ErrorCategory.INVALID_QUERY_ID, "query must be an object")

    query_id = raw.get("query_id", "q")
    if not isinstance(query_id, str) or not _DOC_ID_RE.match(query_id):
        raise LcsError(
            ErrorCategory.INVALID_QUERY_ID,
            f"query_id must match {_DOC_ID_RE.pattern}",
        )

    min_docs = raw.get("min_docs", MIN_DOCS_FLOOR)
    if not isinstance(min_docs, int) or isinstance(min_docs, bool):
        raise LcsError(ErrorCategory.INVALID_MIN_DOCS, "min_docs must be an integer")
    if min_docs < MIN_DOCS_FLOOR:
        raise LcsError(
            ErrorCategory.INVALID_MIN_DOCS,
            f"min_docs must be >= {MIN_DOCS_FLOOR} (coverage is measured in "
            "distinct documents, not occurrences)",
        )
    if min_docs > doc_count:
        raise LcsError(
            ErrorCategory.MIN_DOCS_EXCEEDS_CORPUS,
            f"min_docs={min_docs} exceeds indexed document count {doc_count}",
        )

    max_candidates = raw.get("max_candidates", settings.default_max_candidates)
    if not isinstance(max_candidates, int) or isinstance(max_candidates, bool):
        raise LcsError(
            ErrorCategory.INVALID_MAX_CANDIDATES, "max_candidates must be an integer"
        )
    if not 1 <= max_candidates <= settings.max_candidates_ceiling:
        raise LcsError(
            ErrorCategory.INVALID_MAX_CANDIDATES,
            f"max_candidates must be in [1, {settings.max_candidates_ceiling}]",
        )

    return QuerySpec(query_id=query_id, min_docs=min_docs, max_candidates=max_candidates)


def validate_batch(raw_queries: object) -> list:
    if not isinstance(raw_queries, list) or not raw_queries:
        raise LcsError(ErrorCategory.EMPTY_BATCH, "queries must be a non-empty list")
    return raw_queries
