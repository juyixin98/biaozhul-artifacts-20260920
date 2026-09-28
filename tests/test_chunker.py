"""Chunking tests: boundaries never split a quote/escape/comment."""
from __future__ import annotations

from app.corpus import default_lexicon
from app.mining.chunker import chunk_text
from app.mining.lexer import Lexer


def _roundtrip(text, target):
    lexer = Lexer(default_lexicon())
    chunks = chunk_text(text, lexer, target)
    assert "".join(chunks) == text
    return lexer, chunks


def test_split_preserves_text_plain():
    text = "abcdefghij" * 4
    _, chunks = _roundtrip(text, 7)
    assert all(1 <= len(c) <= 40 for c in chunks)
    assert len(chunks) > 1


def test_boundary_never_cuts_double_quoted_string():
    # A long string spanning what would be two target-sized chunks.
    text = 'ab"([{}]) cdef ghij kl"z'
    lexer, chunks = _roundtrip(text, 6)
    # Re-scanning each block from a clean start must not see the brackets in
    # the string as tokens.
    all_token_offsets = set()
    cursor = 0
    for c in chunks:
        toks, spans = lexer.scan(c)
        for t in toks:
            all_token_offsets.add(cursor + t.offset)
        cursor += len(c)
    # No brackets exist outside the string: token set must be empty.
    assert all_token_offsets == set()


def test_boundary_never_cuts_block_comment():
    text = "x/* ([)] ([)] more */y"
    lexer, chunks = _roundtrip(text, 5)
    assert "".join(chunks) == text
    structural = []
    cursor = 0
    for c in chunks:
        toks, _ = lexer.scan(c)
        structural.extend(cursor + t.offset for t in toks)
        cursor += len(c)
    assert structural == []


def test_chunk_sizes_near_target_when_possible():
    text = "a" * 100
    _, chunks = _roundtrip(text, 10)
    assert [len(c) for c in chunks] == [10] * 10
