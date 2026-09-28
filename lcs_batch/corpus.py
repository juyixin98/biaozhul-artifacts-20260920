"""Corpus specification: documents, separator-safe encoding, offsets.

Encoding invariant
------------------
Documents are arbitrary byte strings. The concatenated text is a sequence of
*integer symbols*, not bytes:

* every content byte ``b`` maps to the symbol ``b`` (range 0..255);
* after document ``i`` we emit a unique separator symbol ``256 + i``.

Because separators are >= 256 they can never collide with content bytes, and
because each separator is unique to its document, no separator symbol ever
appears twice in the stream. Two consequences the kernel relies on:

1. an LCP match between two suffixes can never *contain* a separator, so a
   reported common substring never crosses a document boundary;
2. a suffix starting at a separator can never share a non-empty prefix with
   any other suffix, so separators never pollute candidate sets.

Original (per-document) offsets are recovered as ``pos - doc_starts[doc]``.
"""

from __future__ import annotations

from dataclasses import dataclass

SEPARATOR_BASE = 256
SEPARATOR_DOC_OF = -1  # doc_of marker for separator positions


@dataclass(frozen=True)
class Document:
    doc_id: str
    content: bytes


@dataclass(frozen=True)
class EncodedCorpus:
    documents: tuple[Document, ...]
    symbols: tuple[int, ...]
    doc_of: tuple[int, ...]  # doc index per position, -1 at separators
    doc_starts: tuple[int, ...]  # start position of each doc in the stream

    @property
    def doc_count(self) -> int:
        return len(self.documents)

    def locate(self, position: int) -> tuple[int, int]:
        """Map a stream position to ``(doc_index, offset_within_doc)``."""
        doc_index = self.doc_of[position]
        if doc_index == SEPARATOR_DOC_OF:
            raise ValueError(f"position {position} is a separator")
        return doc_index, position - self.doc_starts[doc_index]


def separator_symbol(doc_index: int) -> int:
    return SEPARATOR_BASE + doc_index


def encode_corpus(documents: list[Document] | tuple[Document, ...]) -> EncodedCorpus:
    """Encode validated documents into the integer symbol stream.

    Structural validation (non-empty, unique ids, size limits) is the caller's
    responsibility (see :mod:`lcs_batch.validation`); this function only
    enforces the encoding invariant that content bytes stay below
    ``SEPARATOR_BASE``.
    """
    if not documents:
        raise ValueError("encode_corpus requires at least one document")

    symbols: list[int] = []
    doc_of: list[int] = []
    doc_starts: list[int] = []
    for index, doc in enumerate(documents):
        if not doc.content:
            raise ValueError(f"document {doc.doc_id!r} is empty")
        for byte in doc.content:
            assert 0 <= byte < SEPARATOR_BASE  # bytes() guarantees this
        doc_starts.append(len(symbols))
        symbols.extend(doc.content)
        doc_of.extend([index] * len(doc.content))
        symbols.append(separator_symbol(index))
        doc_of.append(SEPARATOR_DOC_OF)

    return EncodedCorpus(
        documents=tuple(documents),
        symbols=tuple(symbols),
        doc_of=tuple(doc_of),
        doc_starts=tuple(doc_starts),
    )
