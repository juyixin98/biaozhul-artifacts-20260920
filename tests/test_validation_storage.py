"""Targeted tests for query validation and storage edge branches."""

from __future__ import annotations

import pytest

from entity_resolution.errors import (
    InvalidRequestError,
    RecordNotFoundError,
)
from entity_resolution.validation import parse_run_id, parse_threshold


def test_threshold_default_passthrough() -> None:
    assert parse_threshold(None, 0.42) == 0.42


@pytest.mark.parametrize("bad", ["abc", "0,5", "nan-ish"])
def test_threshold_non_numeric_is_input_error(bad: str) -> None:
    with pytest.raises(InvalidRequestError) as exc:
        parse_threshold(bad, 0.5)
    assert exc.value.details["parameter"] == "threshold"


@pytest.mark.parametrize("bad", ["-0.1", "1.01", "99"])
def test_threshold_out_of_range_is_input_error(bad: str) -> None:
    with pytest.raises(InvalidRequestError):
        parse_threshold(bad, 0.5)


def test_run_id_none_passes_through() -> None:
    assert parse_run_id(None) is None


@pytest.mark.parametrize("bad", ["", "  ", "has space", "tab\there"])
def test_run_id_bad_token_is_input_error(bad: str) -> None:
    with pytest.raises(InvalidRequestError):
        parse_run_id(bad)


def test_storage_get_missing_record_raises(storage) -> None:
    with pytest.raises(RecordNotFoundError):
        storage.get_record("nope")


def test_storage_link_lifecycle(storage) -> None:
    storage.replace_corpus(
        records=[{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
        must=[], cannot=[], aliases={},
    )
    assert storage.add_link("a", "b", "must") is True
    assert storage.add_link("a", "b", "must") is False  # idempotent
    must, cannot = storage.list_links()
    assert ("a", "b") in must
    assert storage.delete_link("a", "b", "must") is True
    assert storage.delete_link("a", "b", "must") is False


def test_storage_persistence_roundtrip(paths) -> None:
    db = paths / "persist.sqlite3"
    from entity_resolution.storage import Storage

    s1 = Storage(db)
    s1.replace_corpus(
        records=[{"id": "a", "name": "Alpha"}], must=[], cannot=[], aliases={}
    )
    s1.ensure_cluster("cl0001")
    s1.apply_solution({"a": "cl0001"}, "run-x")
    s1.close()

    s2 = Storage(db)
    assert s2.get_record("a")["name"] == "Alpha"
    assert s2.assignment() == {"a": "cl0001"}
    assert s2.get_cluster("cl0001")["members"] == ["a"]
    s2.close()
