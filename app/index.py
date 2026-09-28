"""Index assembly and query-time mining over a built index."""

from __future__ import annotations

from dataclasses import dataclass

from app import kernel
from app.corpus import Document, SymbolStream, build_symbol_stream


@dataclass(frozen=True)
class Occurrence:
    doc_id: str
    offset: int  # raw byte offset inside the document


@dataclass(frozen=True)
class Candidate:
    substring: bytes
    doc_coverage: tuple[str, ...]  # sorted distinct document ids
    occurrences: tuple[Occurrence, ...]  # sorted by (doc order, offset); overlaps kept


@dataclass
class Index:
    """Generalized suffix array index over one corpus."""

    stream: SymbolStream
    sa: list[int]
    lcp: list[int]
    doc_ids: list[str]

    @property
    def doc_count(self) -> int:
        return len(self.doc_ids)

    @property
    def total_symbols(self) -> int:
        return len(self.stream.symbols)


def build_index(docs: list[Document]) -> Index:
    stream = build_symbol_stream(docs)
    sa = kernel.build_suffix_array(stream.symbols)
    lcp = kernel.build_lcp(stream.symbols, sa)
    return Index(stream=stream, sa=sa, lcp=lcp, doc_ids=[d.doc_id for d in docs])


def mine_longest(index: Index, min_docs: int) -> tuple[int, list[bytes]]:
    """(length, sorted distinct substrings) covering >= min_docs documents."""
    length = kernel.longest_common_length(index.stream, index.sa, index.lcp, min_docs)
    if length <= 0:
        return 0, []
    candidates = kernel.collect_candidates(
        index.stream, index.sa, index.lcp, min_docs, length
    )
    return length, candidates


def locate(index: Index, pattern: bytes, occurrence_cap: int) -> tuple[Candidate, bool]:
    """Map one candidate substring to its documents and raw byte offsets.

    Overlapping occurrences are all reported (each suffix position is an
    occurrence). Returns (candidate, occurrences_truncated).
    """
    positions = kernel.find_occurrence_positions(index.stream.symbols, index.sa, pattern)
    hits: list[tuple[int, int]] = []  # (doc index, offset)
    for pos in positions:
        doc = index.stream.doc_of[pos]
        if doc < 0:  # a separator can never start a real match; defensive
            continue
        hits.append((doc, pos - index.stream.doc_starts[doc]))
    hits.sort()
    truncated = len(hits) > occurrence_cap
    hits = hits[:occurrence_cap]
    coverage = tuple(index.doc_ids[d] for d in sorted({d for d, _ in hits}))
    occurrences = tuple(Occurrence(doc_id=index.doc_ids[d], offset=off) for d, off in hits)
    return (
        Candidate(substring=pattern, doc_coverage=coverage, occurrences=occurrences),
        truncated,
    )
