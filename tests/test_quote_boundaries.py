"""Quote/escape state crossing chunk boundaries after local edits.

These cases specifically guard the rule "string content is not structure"
when a quote or backslash lands exactly on a chunk boundary, and when a
local edit changes the quote state of later chunks without re-scanning the
whole document.
"""

from __future__ import annotations

from bracket_index.index.store import Store
from bracket_index.index.engine import IndexEngine
from bracket_index.kernel import oracle
from tests.test_oracle_vs_index import assert_index_agrees_with_oracle

# Sized so each bracket sits near a chunk boundary (chunk size 4).
SIZES = [1, 2, 3, 4, 5, 16]


def make_engine(tmp_path, size):
    store = Store(str(tmp_path / f"q-{size}.db"))
    return store, IndexEngine(store, chunk_size=size)


def test_opening_quote_in_one_chunk_shields_later_chunks(tmp_path):
    for size in SIZES:
        store, engine = make_engine(tmp_path, size)
        text = '() "([{)]} tail'  # quote at 3 never closes; rest is content
        doc_id = engine.create_document(text)
        assert_index_agrees_with_oracle(engine, doc_id, text)
        assert engine.balance(doc_id).balanced
        store.close()


def test_quote_insert_invalidates_tail_until_closing_quote(tmp_path):
    for size in SIZES:
        store, engine = make_engine(tmp_path, size)
        original = "a(); b = (x); c = [y] end"
        doc_id = engine.create_document(original)
        # Insert an opening quote before "(x)": "(x); ..." becomes string
        # content up to the closing quote inserted right after 'x'.
        engine.apply_edit(doc_id, 0, 9, 9, '"')   # v1: ...= "(x);...
        engine.apply_edit(doc_id, 1, 13, 13, '"')  # v2: ...= "(x)";...
        edited = 'a(); b = "(x)"; c = [y] end'
        assert_index_agrees_with_oracle(engine, doc_id, edited)
        assert engine.balance(doc_id).balanced
        store.close()


def test_escape_character_split_across_chunks(tmp_path):
    for size in SIZES:
        store, engine = make_engine(tmp_path, size)
        # A backslash run ending exactly on a chunk boundary must not let the
        # next chunk's first quote toggle string state incorrectly.
        text = 'x = "\\\\"; (ok); tail []'
        doc_id = engine.create_document(text)
        assert_index_agrees_with_oracle(engine, doc_id, text)
        store.close()


def test_edit_near_string_does_not_rescan_unaffected_tail(tmp_path):
    size = 4
    store = Store(str(tmp_path / "big.db"))
    engine = IndexEngine(store, chunk_size=size)
    text = "head" + "([{}])" * 40  # 240 chars of balanced blocks
    doc_id = engine.create_document(text)
    result = engine.apply_edit(doc_id, 0, 1, 2, "X")  # replace one char in chunk 0
    # The edit changes no lexer state ("e" -> "X" outside strings), so only
    # the single affected chunk is re-scanned; the 59 tail chunks survive.
    assert result.rescanned_chunks == 1
    assert result.total_chunks == len(text) // size
    edited = "hXad" + "([{}])" * 40
    assert_index_agrees_with_oracle(engine, doc_id, edited)
    store.close()


def test_unterminated_string_edit_then_fix(tmp_path):
    store = Store(str(tmp_path / "fix.db"))
    engine = IndexEngine(store, chunk_size=3)
    doc_id = engine.create_document("(a)(b)(c)")
    # Insert an unterminated quote: all following brackets become content.
    engine.apply_edit(doc_id, 0, 3, 3, '"')
    assert_index_agrees_with_oracle(engine, doc_id, '(a)"(b)(c)')
    balance = engine.balance(doc_id)
    assert balance.balanced  # only "(a)" remains structural
    # Repair: close the quote at the end — brackets were content, stay as-is.
    engine.apply_edit(doc_id, 1, len('(a)"(b)(c)'), len('(a)"(b)(c)'), '"')
    assert_index_agrees_with_oracle(engine, doc_id, '(a)"(b)(c)"')
    store.close()
