"""SQLite repository persistence and relational round-trip tests."""
from __future__ import annotations

import sqlite3

import pytest

from app.corpus.spec import normalize_corpus
from app.errors import ConflictingCorpusError, NotFoundError
from app.storage.repository import CorpusRepository
from tests.conftest import make_request


def _repo(db_path):
    return CorpusRepository(db_path=db_path)


def test_round_trip_preserves_events_positions_and_timestamps(db_path):
    repo = _repo(db_path)
    corpus = normalize_corpus(
        "c1",
        make_request({"s1": [("A", 1.5), ("B", 1.5), ("C", 3.0)]}),
    )
    repo.save_corpus(corpus, has_timestamps=True)

    loaded = repo.get_corpus("c1")
    assert loaded.size == 1
    events = loaded.sequences[0].events
    assert [e.position for e in events] == [0, 1, 2]
    assert [e.symbol for e in events] == ["A", "B", "C"]
    assert [e.timestamp for e in events] == [1.5, 1.5, 3.0]


def test_persistence_across_repository_instances(db_path):
    _repo(db_path).save_corpus(
        normalize_corpus("persist", make_request({"s1": [("X", None)]})),
        has_timestamps=False,
    )
    # A brand-new repository over the same file sees the committed corpus.
    fresh = _repo(db_path)
    assert fresh.get_corpus("persist").sequences[0].events[0].symbol == "X"
    assert fresh.corpus_has_timestamps("persist") is False


def test_duplicate_save_is_conflict(db_path):
    repo = _repo(db_path)
    corpus = normalize_corpus("dup", make_request({"s1": [("A", None)]}))
    repo.save_corpus(corpus, has_timestamps=False)
    with pytest.raises(ConflictingCorpusError) as exc:
        repo.save_corpus(corpus, has_timestamps=False)
    assert exc.value.code == "corpus_conflict"


def test_get_missing_is_not_found(db_path):
    with pytest.raises(NotFoundError) as exc:
        _repo(db_path).get_corpus("ghost")
    assert exc.value.code == "not_found"


def test_delete_cascades_to_events(db_path):
    repo = _repo(db_path)
    repo.save_corpus(
        normalize_corpus("doomed", make_request({
            "s1": [("A", None), ("B", None)],
            "s2": [("C", None)],
        })),
        has_timestamps=False,
    )
    repo.delete_corpus("doomed")
    with pytest.raises(NotFoundError):
        repo.get_corpus("doomed")

    # Child rows must be gone too (no orphaned events/sequences).
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE corpus_id = ?", ("doomed",)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM sequences WHERE corpus_id = ?", ("doomed",)
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_listing_reports_counts(db_path):
    repo = _repo(db_path)
    repo.save_corpus(
        normalize_corpus("a", make_request({"s1": [("A", None), ("B", None)]})),
        has_timestamps=False,
    )
    repo.save_corpus(
        normalize_corpus("b", make_request({
            "x": [("A", None)], "y": [("A", None), ("B", None)],
        })),
        has_timestamps=False,
    )
    listing = {row["corpus_id"]: row for row in repo.list_corpora()}
    assert listing["a"]["seq_count"] == 1
    assert listing["a"]["event_count"] == 2
    assert listing["b"]["seq_count"] == 2
    assert listing["b"]["event_count"] == 3


def test_corpus_has_timestamps_missing_is_not_found(db_path):
    with pytest.raises(NotFoundError) as exc:
        _repo(db_path).corpus_has_timestamps("ghost")
    assert exc.value.code == "not_found"


def test_delete_missing_is_not_found(db_path):
    with pytest.raises(NotFoundError):
        _repo(db_path).delete_corpus("ghost")
