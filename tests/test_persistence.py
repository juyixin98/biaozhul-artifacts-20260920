"""持久化层：往返一致性与引用校验（契约 4：拒绝环与悬空状态）。"""

import sqlite3

import pytest

from minidfa import (
    AutomatonNotFoundError,
    CycleDetectedError,
    DanglingReferenceError,
    UnreachableStateError,
    build_minimal_dfa,
)
from minidfa.persistence import AutomatonStore

WORDS = ["ape", "apple", "applet", "banana", "band", "bandit"]


@pytest.fixture()
def store(tmp_path):
    s = AutomatonStore(tmp_path / "store.db")
    yield s
    s.close()


def test_roundtrip_preserves_language_and_counts(store):
    dfa = build_minimal_dfa(WORDS)
    store.save(dfa, "dict", fingerprint="fp1")

    loaded = store.load("dict")
    assert loaded.state_count == dfa.state_count
    assert sorted(loaded.iter_words()) == WORDS
    for prefix in ["", "a", "ap", "app", "ban", "band", "zzz"]:
        assert loaded.prefix_count(prefix) == dfa.prefix_count(prefix)


def test_multiple_named_automata_isolated(store):
    store.save(build_minimal_dfa(["a", "b"]), "one", fingerprint="f1")
    store.save(build_minimal_dfa(["x", "y", "z"]), "two", fingerprint="f2")
    assert store.list_names() == ["one", "two"]
    assert store.load("one").contains("a")
    assert not store.load("one").contains("x")
    assert store.load("two").contains("z")


def test_load_unknown_name(store):
    with pytest.raises(AutomatonNotFoundError) as exc_info:
        store.load("nope")
    assert exc_info.value.category == "AUTOMATON_NOT_FOUND"


def _insert_automaton_row(db_path, name="evil"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO automata VALUES (?, 'fp', 0, 0, 0, '1', 'test')", (name,)
    )
    conn.commit()
    return conn


def test_dangling_target_rejected(store):
    store.save(build_minimal_dfa(["ab"]), "d", fingerprint="f")
    conn = sqlite3.connect(store.db_path)
    conn.execute("INSERT INTO transitions VALUES ('d', 0, 'z', 999)")
    conn.commit()
    conn.close()
    with pytest.raises(DanglingReferenceError) as exc_info:
        store.load("d")
    assert exc_info.value.category == "DANGLING_REFERENCE"


def test_dangling_source_rejected(store):
    store.save(build_minimal_dfa(["ab"]), "d", fingerprint="f")
    conn = sqlite3.connect(store.db_path)
    conn.execute("INSERT INTO transitions VALUES ('d', 999, 'z', 0)")
    conn.commit()
    conn.close()
    with pytest.raises(DanglingReferenceError):
        store.load("d")


def test_cycle_rejected(store):
    conn = _insert_automaton_row(store.db_path)
    conn.executemany("INSERT INTO states VALUES ('evil', ?, 0)", [(0,), (1,)])
    conn.executemany(
        "INSERT INTO transitions VALUES ('evil', ?, ?, ?)", [(0, "a", 1), (1, "b", 0)]
    )
    conn.commit()
    conn.close()
    with pytest.raises(CycleDetectedError) as exc_info:
        store.load("evil")
    assert exc_info.value.category == "CYCLE_DETECTED"


def test_unreachable_state_rejected(store):
    conn = _insert_automaton_row(store.db_path)
    conn.executemany("INSERT INTO states VALUES ('evil', ?, ?)", [(0, 0), (1, 0), (2, 1)])
    conn.execute("INSERT INTO transitions VALUES ('evil', 0, 'a', 1)")
    conn.commit()
    conn.close()
    with pytest.raises(UnreachableStateError) as exc_info:
        store.load("evil")
    assert exc_info.value.category == "UNREACHABLE_STATE"
    assert exc_info.value.detail["unreachable"] == [2]


def test_delete(store):
    store.save(build_minimal_dfa(["a"]), "gone", fingerprint="f")
    store.delete("gone")
    assert not store.exists("gone")
    with pytest.raises(AutomatonNotFoundError):
        store.delete("gone")
