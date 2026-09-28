"""Edit-path tests: local invalidation, offset versioning, oracle agreement."""

from __future__ import annotations

import pytest

from bracket_index.index.engine import CATEGORY_MATCHED, IndexEngine
from bracket_index.index.errors import (
    InvalidRangeError,
    StaleVersionError,
)
from bracket_index.kernel import oracle
from tests.test_oracle_vs_index import assert_index_agrees_with_oracle

BASE = "head(a)[b]{c} middle(x{y[z]w}) tail(p[q]r)"


def test_insert_invalidates_only_affected_chunks(engine):
    doc_id = engine.create_document(BASE)
    result = engine.apply_edit(doc_id, 0, 10, 10, "INSERT")
    assert result.version == 1
    # The edit touches one chunk region; the rest of the document is not
    # re-scanned (tail chunks are only renumbered).
    assert result.rescanned_chunks <= 3
    assert result.total_chunks > result.rescanned_chunks


def test_insert_then_queries_match_oracle(engine):
    doc_id = engine.create_document(BASE)
    new_text = BASE[:10] + "INSERT(x)" + BASE[10:]
    engine.apply_edit(doc_id, 0, 10, 10, "INSERT(x)")
    assert engine.length_of(doc_id) == len(new_text)
    assert_index_agrees_with_oracle(engine, doc_id, new_text)


def test_delete_spanning_chunks_matches_oracle(engine):
    doc_id = engine.create_document(BASE)
    new_text = BASE[:5] + BASE[30:]
    engine.apply_edit(doc_id, 0, 5, 30, "")
    assert engine.length_of(doc_id) == len(new_text)
    assert_index_agrees_with_oracle(engine, doc_id, new_text)


def test_delete_entire_document(engine):
    doc_id = engine.create_document(BASE)
    engine.apply_edit(doc_id, 0, 0, len(BASE), "")
    assert engine.length_of(doc_id) == 0
    assert engine.balance(doc_id).balanced


def test_edit_into_empty_document(engine):
    doc_id = engine.create_document("")
    engine.apply_edit(doc_id, 0, 0, 0, "(new)")
    assert_index_agrees_with_oracle(engine, doc_id, "(new)")


def test_stale_version_is_rejected_and_document_untouched(engine):
    doc_id = engine.create_document(BASE)
    engine.apply_edit(doc_id, 0, 0, 0, "v1 ")
    with pytest.raises(StaleVersionError) as exc_info:
        engine.apply_edit(doc_id, 0, 0, 0, "stale")  # version is now 1
    assert exc_info.value.expected == 0
    assert exc_info.value.actual == 1
    # The rejected edit changed nothing.
    assert_index_agrees_with_oracle(engine, doc_id, "v1 " + BASE)


def test_out_of_range_edit_is_rejected(engine):
    doc_id = engine.create_document(BASE)
    with pytest.raises(InvalidRangeError):
        engine.apply_edit(doc_id, 0, 0, len(BASE) + 1, "")
    with pytest.raises(InvalidRangeError):
        engine.apply_edit(doc_id, 0, 10, 5, "")


def test_sequential_edits_stay_consistent_with_oracle(engine):
    doc_id = engine.create_document(BASE)
    text = BASE
    edits = [
        (4, 5, "((("),   # insert extra opens before (a)
        (0, 4, ""),       # delete "head"
        (20, 20, "{m}"),  # insert balanced block mid-document
        (2, 5, "]"),      # replace with a stray closer -> unbalanced
    ]
    version = 0
    for start, end, replacement in edits:
        engine.apply_edit(doc_id, version, start, end, replacement)
        text = text[:start] + replacement + text[end:]
        version += 1
        assert engine.length_of(doc_id) == len(text)
        assert_index_agrees_with_oracle(engine, doc_id, text)


def test_match_positions_shift_correctly_after_insert(engine):
    text = "a(b)c(d)e"
    doc_id = engine.create_document(text)
    before = engine.match(doc_id, 1)
    assert (before.category, before.match_pos) == (CATEGORY_MATCHED, 3)
    engine.apply_edit(doc_id, 0, 0, 0, "PREFIX")
    shifted = engine.match(doc_id, 1 + 6)
    assert (shifted.category, shifted.match_pos) == (CATEGORY_MATCHED, 3 + 6)
    # The old offset now points at different text.
    stale = oracle.scan("PREFIX" + text)
    assert 1 not in stale.pairs or stale.pairs.get(1) != 3
