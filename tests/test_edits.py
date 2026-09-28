"""Local edit, invalidation scope and offset-version tests."""
from __future__ import annotations

import random

import pytest

from app.corpus import default_lexicon, synthetic_document
from app.mining.lexer import Lexer
from app.service import VERSION_CONFLICT, DocumentService, ServiceError
from app.storage import Database

from tests.fixtures.oracle import oracle_scan


@pytest.fixture
def svc(tmp_path):
    db = Database(str(tmp_path / "edit.db"))
    service = DocumentService(db, lexicon=default_lexicon(), chunk_size=16)
    yield service
    db.close()


def _create(svc, name, text):
    rec = svc.create_document(name, text)
    return rec.id


def test_insert_and_delete_agree_with_oracle(svc):
    text = "function(a, [b, c], {d: 1}) // tail"
    doc_id = _create(svc, "d1", text)
    v = 1
    # Insert nested structure in the middle.
    r = svc.edit(doc_id, 9, 9, "([", v); v = r.version
    text = text[:9] + "([" + text[9:]
    assert svc.verify_against_full_scan(doc_id)["agrees"]
    # Delete a range that includes structural chars.
    r = svc.edit(doc_id, 4, 12, "", v); v = r.version
    text = text[:4] + text[12:]
    assert svc.verify_against_full_scan(doc_id)["agrees"]
    _assert_vs_oracle(svc, doc_id, text)


def test_stale_version_edit_is_rejected_and_state_unchanged(svc):
    doc_id = _create(svc, "v", "(abc)")
    first = svc.edit(doc_id, 0, 0, "[", 1)
    assert first.version == 2
    before = svc.text_of(doc_id)
    with pytest.raises(ServiceError) as exc:
        svc.edit(doc_id, 1, 1, "x", 1)  # stale base version
    assert exc.value.category == VERSION_CONFLICT
    assert svc.text_of(doc_id) == before
    assert svc.get(doc_id).version == 2


def test_range_out_of_bounds_rejected(svc):
    doc_id = _create(svc, "b", "abc")
    with pytest.raises(ServiceError) as exc:
        svc.edit(doc_id, 2, 5, "x", 1)
    assert exc.value.category == "OFFSET_OUT_OF_RANGE"


def test_edit_only_invalidates_affected_blocks(svc):
    text = synthetic_document(seed=3, target_length=1200)
    doc_id = _create(svc, "big", text)
    treap = svc._treap(doc_id)
    before = treap.leaf_segments()
    assert len(before) > 20

    edit_start = 600
    report = svc.edit(doc_id, edit_start, edit_start, "x", 1)
    win_lo, win_hi = report.window
    after = treap.leaf_segments()
    before_by_id = {nid: (start, txt) for nid, start, txt in before}
    after_by_id = {nid: (start, txt) for nid, start, txt in after}
    surviving = set(before_by_id) & set(after_by_id)

    # The invalidated window is local: a small fraction of the document.
    assert win_hi - win_lo < len(text) // 4
    assert report.rescanned_chars < len(text) // 4

    # Surviving blocks strictly BEFORE the window keep id, text AND start.
    before_keepers = [
        nid for nid in surviving if before_by_id[nid][0] < win_lo
    ]
    assert before_keepers, "expected untouched blocks before the window"
    for nid in before_keepers:
        assert after_by_id[nid] == before_by_id[nid]

    # Surviving blocks strictly AFTER the window keep id and text; their
    # start shifts exactly by the edit's net length (+1 here).
    after_keepers = [
        nid for nid in surviving
        if before_by_id[nid][0] >= win_hi
    ]
    assert after_keepers, "expected untouched blocks after the window"
    for nid in after_keepers:
        old_start, old_text = before_by_id[nid]
        new_start, new_text = after_by_id[nid]
        assert new_text == old_text
        assert new_start == old_start + 1

    # Most blocks survived untouched: only the local window was rebuilt.
    assert len(surviving) >= len(before) - report.blocks_removed


def test_edit_in_string_only_absorbs_until_mask_closes(svc):
    # Quote deliberately spans multiple target chunks.
    text = 'ab"cccccccccccccccc([)]cccccccccccccccc"tail(x)'
    doc_id = _create(svc, "s", text)
    assert svc.verify_against_full_scan(doc_id)["agrees"]
    # Delete the OPENING quote: previously masked brackets become structural;
    # the edit pipeline must absorb the whole former string, not just its
    # block, or the result silently disagrees with a full scan.
    report = svc.edit(doc_id, 2, 3, "", 1)
    assert report.mask_blocks_absorbed >= 1
    assert svc.verify_against_full_scan(doc_id)["agrees"]
    expected = text[:2] + text[3:]
    assert svc.text_of(doc_id) == expected
    _assert_vs_oracle(svc, doc_id, expected)


def test_random_edit_sequence_always_agrees_with_oracle(svc):
    rng = random.Random(7)
    text = synthetic_document(seed=11, target_length=900)
    doc_id = _create(svc, "rand", text)
    version = 1
    for step in range(40):
        length = len(text)
        start = rng.randint(0, length)
        end = rng.randint(start, min(length, start + rng.choice([0, 1, 3, 20])))
        choice = rng.choice(["insert_pair", "insert_text", "delete",
                             "quote_char", "plain"])
        replacement = {
            "insert_pair": "([])",
            "insert_text": "zzz",
            "delete": "",
            "quote_char": '"',
            "plain": "q",
        }[choice]
        report = svc.edit(doc_id, start, end, replacement, version)
        version = report.version
        text = text[:start] + replacement + text[end:]
        assert svc.text_of(doc_id) == text
        assert svc.verify_against_full_scan(doc_id)["agrees"]
        _assert_vs_oracle(svc, doc_id, text)


def test_persistence_roundtrip(tmp_path):
    path = str(tmp_path / "persist.db")
    db = Database(path)
    svc = DocumentService(db, lexicon=default_lexicon(), chunk_size=12)
    rec = svc.create_document("persisted", "([{x}])" + "y" * 50)
    db.close()

    db2 = Database(path)
    svc2 = DocumentService(db2, lexicon=default_lexicon(), chunk_size=12)
    assert svc2.get(rec.id).version == 1
    assert svc2.verify_against_full_scan(rec.id)["agrees"]
    db2.close()


def _assert_vs_oracle(svc, doc_id, text):
    result = svc.analyze(doc_id)
    oracle = oracle_scan(text)
    assert sorted((o.offset, c.offset, o.type) for o, c in result.matches) \
        == sorted(oracle.matches)
    assert sorted((o.offset, c.offset) for o, c in result.mismatches) \
        == sorted((o, c) for o, c, _, _ in oracle.mismatches)
    assert [t.offset for t in result.stray_closes] == list(oracle.stray_closes)
    assert [t.offset for t in result.stray_opens] == list(oracle.stray_opens)
