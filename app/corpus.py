"""Corpus specification: document decoding, validation, and symbol-stream layout.

Key design decision — separator safety:
    Documents are raw bytes (binary-safe). The concatenated stream is built
    over *integers*, not bytes: content byte ``b`` maps to symbol ``b``
    (0..255) and the separator after document ``i`` is the symbol
    ``SEPARATOR_BASE + i`` (>= 256). Separators therefore live in a disjoint
    integer space that no document byte can ever occupy, and each separator
    is unique per document slot. A spurious match crossing a document
    boundary is impossible *by construction*, including for documents that
    contain all 256 byte values.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

from app.config import Settings
from app.errors import AppError, FailureCategory

#: Content symbols occupy 0..255; separators start here. Disjoint by design.
SEPARATOR_BASE = 256


@dataclass(frozen=True)
class Document:
    doc_id: str
    content: bytes


@dataclass(frozen=True)
class SymbolStream:
    """Concatenated corpus plus per-position document mapping."""

    symbols: list[int]  # content bytes as 0..255, separators as SEPARATOR_BASE + slot
    doc_of: list[int]  # doc index per position, -1 on separators
    extent: list[int]  # distance from position to its document's end (0 on separators)
    doc_starts: list[int]  # stream offset where each document begins


def decode_document(doc_id: str, content_b64: str) -> Document:
    """Decode one base64 document payload into raw bytes."""
    if not doc_id or not doc_id.strip():
        raise AppError(FailureCategory.EMPTY_DOCUMENT_ID, "document id must be non-empty")
    try:
        content = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AppError(
            FailureCategory.INVALID_BASE64,
            f"document {doc_id!r} is not valid base64: {exc}",
        ) from exc
    return Document(doc_id=doc_id, content=content)


def validate_documents(docs: list[Document], settings: Settings) -> None:
    """Validate a decoded corpus against the configured limits."""
    if not docs:
        raise AppError(FailureCategory.EMPTY_QUERY_BATCH, "corpus must contain documents")
    if len(docs) > settings.max_documents:
        raise AppError(
            FailureCategory.TOO_MANY_DOCUMENTS,
            f"{len(docs)} documents exceeds limit {settings.max_documents}",
        )
    seen: set[str] = set()
    total = 0
    for doc in docs:
        if doc.doc_id in seen:
            raise AppError(
                FailureCategory.DUPLICATE_DOC_ID,
                f"duplicate document id {doc.doc_id!r}",
            )
        seen.add(doc.doc_id)
        if len(doc.content) == 0:
            raise AppError(
                FailureCategory.EMPTY_DOCUMENT,
                f"document {doc.doc_id!r} is empty",
            )
        if len(doc.content) > settings.max_document_bytes:
            raise AppError(
                FailureCategory.DOCUMENT_TOO_LARGE,
                f"document {doc.doc_id!r} has {len(doc.content)} bytes "
                f"(limit {settings.max_document_bytes})",
            )
        total += len(doc.content)
    if total > settings.max_total_bytes:
        raise AppError(
            FailureCategory.CORPUS_TOO_LARGE,
            f"corpus of {total} bytes exceeds limit {settings.max_total_bytes}",
        )


def build_symbol_stream(docs: list[Document]) -> SymbolStream:
    """Concatenate documents into the separated integer symbol stream."""
    symbols: list[int] = []
    doc_of: list[int] = []
    doc_starts: list[int] = []
    doc_ends: list[int] = []
    for idx, doc in enumerate(docs):
        doc_starts.append(len(symbols))
        symbols.extend(doc.content)
        doc_of.extend([idx] * len(doc.content))
        doc_ends.append(len(symbols))
        if idx < len(docs) - 1:
            symbols.append(SEPARATOR_BASE + idx)
            doc_of.append(-1)
    extent = [0] * len(symbols)
    for idx in range(len(docs)):
        start, end = doc_starts[idx], doc_ends[idx]
        for pos in range(start, end):
            extent[pos] = end - pos
    return SymbolStream(
        symbols=symbols, doc_of=doc_of, extent=extent, doc_starts=doc_starts
    )
