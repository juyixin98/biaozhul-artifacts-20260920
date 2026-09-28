"""Binary payloads: every byte value 0x00..0xFF must round-trip safely."""

from app.corpus import Document
from app.index import build_index, locate, mine_longest

ALL_BYTES = bytes(range(256))


def test_full_byte_range_shared_between_documents():
    docs = [
        Document("bin_a", ALL_BYTES + b"TAIL"),
        Document("bin_b", b"HEAD" + ALL_BYTES),
    ]
    index = build_index(docs)
    length, candidates = mine_longest(index, min_docs=2)
    assert length == 256
    assert candidates == [ALL_BYTES]
    candidate, _ = locate(index, ALL_BYTES, occurrence_cap=100)
    assert [(o.doc_id, o.offset) for o in candidate.occurrences] == [
        ("bin_a", 0),
        ("bin_b", 4),
    ]


def test_separator_adjacent_bytes_in_content():
    # 0x00 and 0xFF sit at the edges of the content symbol space; they must
    # behave as ordinary content, never as separators.
    docs = [
        Document("nul", b"\x00\xff\x00\xff"),
        Document("high", b"\xff\x00\xff\x00"),
    ]
    index = build_index(docs)
    length, candidates = mine_longest(index, min_docs=2)
    assert length == 3
    assert candidates == [b"\x00\xff\x00", b"\xff\x00\xff"]


def test_binary_roundtrip_through_store_and_reload(tmp_path):
    from app.store import CorpusStore

    store = CorpusStore(str(tmp_path / "bin.db"))
    docs = [Document("blob", ALL_BYTES), Document("blob2", ALL_BYTES[::-1])]
    index = build_index(docs)
    meta = store.save_corpus("binary", docs, index)
    reloaded = store.load_index(meta["corpus_id"])
    length, candidates = mine_longest(reloaded, min_docs=2)
    # longest run shared by a sequence and its reverse: the palindrome-free
    # byte range shares exactly one byte pair... verify against brute force.
    from tests.brute_force import brute_longest

    expected_length, expected_candidates = brute_longest(
        [ALL_BYTES, ALL_BYTES[::-1]], min_docs=2
    )
    assert length == expected_length
    assert candidates == expected_candidates
