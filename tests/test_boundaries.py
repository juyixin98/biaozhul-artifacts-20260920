"""Document boundary safety: matches must never span the separator."""

from app.corpus import SEPARATOR_BASE, Document, build_symbol_stream
from app.index import build_index, locate, mine_longest


def test_separator_symbols_are_disjoint_from_all_byte_values():
    docs = [Document("a", bytes(range(256))), Document("b", b"x")]
    stream = build_symbol_stream(docs)
    separators = {s for s in stream.symbols if s >= SEPARATOR_BASE}
    content = {s for s, d in zip(stream.symbols, stream.doc_of) if d >= 0}
    assert separators  # there is exactly one separator for two docs
    assert separators.isdisjoint(content)
    assert len(separators) == 1  # unique per document slot


def test_no_match_crosses_boundary_when_docs_are_reversed():
    # d1="ab", d2="ba": naive concatenation "abba" would suggest length-2
    # structure; the true longest substring common to both docs is length 1.
    docs = [Document("d1", b"ab"), Document("d2", b"ba")]
    index = build_index(docs)
    length, candidates = mine_longest(index, min_docs=2)
    assert length == 1
    assert candidates == [b"a", b"b"]


def test_suffix_of_one_doc_prefix_of_next_does_not_extend_match():
    # d1 ends with "abc", d2 starts with "abc": a boundary leak would
    # fabricate "abcabc" across the join; the true common part is "abc".
    docs = [Document("left", b"ZZabc"), Document("right", b"abcZZ")]
    index = build_index(docs)
    length, candidates = mine_longest(index, min_docs=2)
    assert length == 3
    assert candidates == [b"abc"]
    candidate, _ = locate(index, b"abc", occurrence_cap=100)
    assert [(o.doc_id, o.offset) for o in candidate.occurrences] == [
        ("left", 2),
        ("right", 0),
    ]


def test_every_reported_occurrence_stays_inside_its_document():
    docs = [
        Document("a", b"xxCOMMONxx"),
        Document("b", b"COMMON"),
        Document("c", b"yCOMMONyCOMMONy"),
    ]
    index = build_index(docs)
    length, candidates = mine_longest(index, min_docs=3)
    assert (length, candidates) == (6, [b"COMMON"])
    raw = {d.doc_id: d.content for d in docs}
    candidate, _ = locate(index, b"COMMON", occurrence_cap=100)
    for occ in candidate.occurrences:
        content = raw[occ.doc_id]
        assert 0 <= occ.offset
        assert occ.offset + length <= len(content)
        assert content[occ.offset : occ.offset + length] == b"COMMON"
