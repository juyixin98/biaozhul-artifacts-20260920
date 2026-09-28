"""Corpus spec: separator safety and encoding invariants."""

import pytest

from lcs_batch.corpus import (
    SEPARATOR_BASE,
    SEPARATOR_DOC_OF,
    Document,
    encode_corpus,
    separator_symbol,
)


def test_separator_never_collides_with_any_content_byte():
    # A document containing every possible byte value, including 0x00 and 0xFF.
    all_bytes = bytes(range(256))
    corpus = encode_corpus([Document("d1", all_bytes), Document("d2", all_bytes)])

    content_symbols = {s for s, d in zip(corpus.symbols, corpus.doc_of) if d != SEPARATOR_DOC_OF}
    separator_symbols = {s for s, d in zip(corpus.symbols, corpus.doc_of) if d == SEPARATOR_DOC_OF}

    assert content_symbols == set(range(256))
    assert separator_symbols == {separator_symbol(0), separator_symbol(1)}
    assert content_symbols.isdisjoint(separator_symbols)
    assert all(s >= SEPARATOR_BASE for s in separator_symbols)


def test_each_document_gets_a_unique_separator():
    corpus = encode_corpus([Document(f"d{i}", b"x") for i in range(5)])
    separators = [s for s, d in zip(corpus.symbols, corpus.doc_of) if d == SEPARATOR_DOC_OF]
    assert len(separators) == 5
    assert len(set(separators)) == 5  # unique per document


def test_stream_layout_and_original_offsets():
    corpus = encode_corpus([Document("a", b"xy"), Document("b", b"zzz")])
    assert corpus.symbols == (
        ord("x"), ord("y"), separator_symbol(0),
        ord("z"), ord("z"), ord("z"), separator_symbol(1),
    )
    assert corpus.doc_starts == (0, 3)
    # positions map back to original per-document offsets
    assert corpus.locate(0) == (0, 0)
    assert corpus.locate(1) == (0, 1)
    assert corpus.locate(3) == (1, 0)
    assert corpus.locate(5) == (1, 2)


def test_locate_rejects_separator_positions():
    corpus = encode_corpus([Document("a", b"xy"), Document("b", b"z")])
    with pytest.raises(ValueError):
        corpus.locate(2)  # separator after doc "a"


def test_encode_rejects_empty_inputs():
    with pytest.raises(ValueError):
        encode_corpus([])
    with pytest.raises(ValueError):
        encode_corpus([Document("a", b"")])
