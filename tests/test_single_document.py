"""Repeated single-document scenarios."""

from app.corpus import Document
from app.index import build_index, locate, mine_longest


def test_single_document_repeated_whole_content_is_longest():
    # One document, min_docs=1: the longest substring covering >= 1 document
    # is the document itself — the match must not leak past the document end.
    docs = [Document("only", b"abcabcabc")]
    index = build_index(docs)
    length, candidates = mine_longest(index, min_docs=1)
    assert length == 9
    assert candidates == [b"abcabcabc"]


def test_same_document_uploaded_twice_matches_in_full():
    # The same bytes as two documents: full content is common to both,
    # coverage must list both documents, offsets are per-document.
    docs = [Document("copy_a", b"shared-payload"), Document("copy_b", b"shared-payload")]
    index = build_index(docs)
    length, candidates = mine_longest(index, min_docs=2)
    assert length == len(b"shared-payload")
    assert candidates == [b"shared-payload"]
    candidate, truncated = locate(index, candidates[0], occurrence_cap=100)
    assert not truncated
    assert candidate.doc_coverage == ("copy_a", "copy_b")
    assert [(o.doc_id, o.offset) for o in candidate.occurrences] == [
        ("copy_a", 0),
        ("copy_b", 0),
    ]


def test_internal_repetition_reported_with_overlapping_offsets():
    # "aaa" occurs at offsets 0,1,2 (overlapping) in "aaaa" — all kept.
    docs = [Document("rep", b"aaaa"), Document("other", b"aaaZ")]
    index = build_index(docs)
    length, candidates = mine_longest(index, min_docs=2)
    assert length == 3
    assert candidates == [b"aaa"]
    candidate, _ = locate(index, b"aaa", occurrence_cap=100)
    assert [(o.doc_id, o.offset) for o in candidate.occurrences] == [
        ("rep", 0),
        ("rep", 1),
        ("other", 0),
    ]
