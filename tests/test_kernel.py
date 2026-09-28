"""Kernel acceptance tests.

These pin CONCRETE results from the hand-authored fixture set and cross-check
the indexed pipeline against the independently implemented oracle and against
the product's own complete stack scan. The reference answers do not come from
the chunk composition code under test.
"""
from __future__ import annotations

import pytest

from app.corpus import default_lexicon, synthetic_document
from app.mining.index import BlockTreap
from app.mining.lexer import Lexer
from app.mining.scanner import scan_tokens
from app.service import DocumentService, reduction_to_structure
from app.storage import Database, DocumentRepository

from tests.fixtures.cases import HAND_CASES, MATCH_CASES
from tests.fixtures.oracle import oracle_scan


@pytest.fixture
def lexer():
    return Lexer(default_lexicon())


def _full_scan(lexer, text):
    tokens, _ = lexer.scan(text)
    return scan_tokens(tokens, len(text))


_probe_serial = 0


def _index_scan(db, lexer, text, chunk_size):
    """Build an indexed document and return its root analysis.

    A real documents row is required by the block-table foreign key.
    """
    global _probe_serial
    _probe_serial += 1
    doc_id = DocumentRepository(db).create_document(
        f"probe_{_probe_serial}", "default"
    )
    treap = BlockTreap(db, doc_id, lexer, chunk_size)
    treap.build(text)
    assert treap.full_text() == text
    return reduction_to_structure(treap.root_reduction(), len(text))


@pytest.mark.parametrize("label,text,expected", HAND_CASES,
                         ids=[c[0] for c in HAND_CASES])
def test_full_scan_matches_hand_expectations(lexer, label, text, expected):
    result = _full_scan(lexer, text)
    assert (
        sorted((o.offset, c.offset, o.type) for o, c in result.matches)
        == sorted(expected["matches"])
    )
    assert (
        sorted((o.offset, c.offset) for o, c in result.mismatches)
        == sorted(expected["mismatches"])
    )
    assert [t.offset for t in result.stray_opens] == expected["stray_open"]
    assert [t.offset for t in result.stray_closes] == expected["stray_close"]
    assert result.balanced is expected["balanced"]


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 16, 64])
@pytest.mark.parametrize("label,text,_expected", HAND_CASES,
                         ids=[c[0] for c in HAND_CASES])
def test_indexed_pipeline_agrees_with_full_scan(
    tmp_path, lexer, chunk_size, label, text, _expected
):
    db = Database(str(tmp_path / f"k_{chunk_size}_{label}.db"))
    try:
        indexed = _index_scan(db, lexer, text, chunk_size)
        full = _full_scan(lexer, text)
        assert _sigs(indexed) == _sigs(full)
    finally:
        db.close()


@pytest.mark.parametrize("chunk_size", [1, 4, 16])
def test_indexed_pipeline_agrees_with_independent_oracle_on_synthetic(
    tmp_path, lexer, chunk_size
):
    db = Database(str(tmp_path / "syn.db"))
    try:
        for seed in range(6):
            text = synthetic_document(
                seed=seed, target_length=600,
                include_strings=True, include_comments=True,
            )
            indexed = _index_scan(db, lexer, text, chunk_size)
            oracle = oracle_scan(text)
            assert (
                sorted((o.offset, c.offset, o.type)
                       for o, c in indexed.matches)
                == sorted(oracle.matches)
            )
            assert (
                sorted((o.offset, c.offset) for o, c in indexed.mismatches)
                == sorted((o, c) for o, c, _, _ in oracle.mismatches)
            )
            assert [t.offset for t in indexed.stray_closes] \
                == list(oracle.stray_closes)
            assert [t.offset for t in indexed.stray_opens] \
                == list(oracle.stray_opens)
    finally:
        db.close()


@pytest.mark.parametrize("text,expectations", MATCH_CASES)
def test_match_jump_exact_positions(lexer, text, expectations):
    result = _full_scan(lexer, text)
    for offset, partner in expectations.items():
        pair = result.match_for(offset)
        assert pair is not None, offset
        opener, closer = pair
        assert (closer.offset if opener.offset == offset else opener.offset) \
            == partner


def test_multi_chunk_nesting_depth(lexer, tmp_path):
    text = "(" * 50 + ")" * 50
    db = Database(str(tmp_path / "deep.db"))
    try:
        indexed = _index_scan(db, lexer, text, 7)
        assert indexed.balanced
        assert len(indexed.matches) == 50
    finally:
        db.close()


def _sigs(result):
    return (
        sorted((o.offset, c.offset, o.type) for o, c in result.matches),
        sorted((o.offset, c.offset) for o, c in result.mismatches),
        [t.offset for t in result.stray_closes],
        [t.offset for t in result.stray_opens],
    )
