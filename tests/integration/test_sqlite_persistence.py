"""SQLite 持久化集成测试：往返一致性与损坏注入。"""

from __future__ import annotations

import sqlite3

import pytest

from app.core.dawg import build_dawg
from app.corpus.spec import CorpusSpec
from app.index.errors import IndexIntegrityError
from app.index.repository import SQLiteIndexRepository
from app.index.service import IndexService
from app.index.validator import (
    VIOLATION_CYCLE,
    VIOLATION_DANGLING_EDGE,
    VIOLATION_UNREACHABLE_STATE,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def repo(tmp_path) -> SQLiteIndexRepository:
    return SQLiteIndexRepository(tmp_path / "idx.sqlite3")


def _build_persisted(service: IndexService):
    words = ["cat", "cats", "dog", "dogs", "walk", "walks"]
    return service.build_from_words(words, index_name="it", spec=CorpusSpec())


def test_save_then_load_roundtrip(repo: SQLiteIndexRepository) -> None:
    service = IndexService(repo)
    dawg, report = _build_persisted(service)
    assert report.metadata.word_count == 6

    loaded = service.load()
    assert loaded.metadata.index_name == "it"
    assert loaded.metadata.state_count == 10
    assert loaded.metadata.word_count == 6
    # 重新加载后的自动机与构建产物接受同一语言、相同前缀计数。
    for word in ["cat", "cats", "dog", "dogs", "walk", "walks"]:
        assert loaded.dawg.contains(word) is True
    assert loaded.dawg.contains("catss") is False
    assert loaded.dawg.prefix_count("cat") == 2
    assert loaded.dawg.prefix_count("") == 6
    assert loaded.dawg.total_words() == 6


def test_persistence_uses_compact_state_ids_and_root_zero(repo) -> None:
    service = IndexService(repo)
    _build_persisted(service)
    bundle = repo.load_bundle()
    ids = [s.state_id for s in bundle.states]
    assert ids == list(range(len(ids)))
    assert ids[0] == 0


def test_load_without_build_reports_integrity_not_success(tmp_path) -> None:
    # 空库（有表无元数据）必须明确失败，而不是返回空成功。
    empty_repo = SQLiteIndexRepository(tmp_path / "empty.sqlite3")
    empty_repo._connect().close()  # 仅创建文件，不建数据
    service = IndexService(empty_repo)
    with pytest.raises(IndexIntegrityError) as exc:
        service.load()
    assert exc.value.violations[0].kind == "metadata_missing"


def test_dangling_edge_injected_at_storage_level_is_rejected(repo) -> None:
    service = IndexService(repo)
    _build_persisted(service)

    # 直接在 SQLite 层插入一条指向不存在状态的边（绕过应用层）。
    conn = sqlite3.connect(str(repo.path))
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("INSERT INTO edges (source, symbol, target) VALUES (0, 'Q', 9999)")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(IndexIntegrityError) as exc:
        repo.load_dawg()
    kinds = {v.kind for v in exc.value.violations}
    assert VIOLATION_DANGLING_EDGE in kinds


def test_unreachable_state_injected_is_rejected(repo) -> None:
    service = IndexService(repo)
    _build_persisted(service)

    conn = sqlite3.connect(str(repo.path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        max_id = conn.execute("SELECT MAX(state_id) FROM states").fetchone()[0]
        new_id = max_id + 1
        conn.execute(
            "INSERT INTO states (state_id, is_final, word_count) VALUES (?, 1, 1)",
            (new_id,),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(IndexIntegrityError) as exc:
        repo.load_dawg()
    assert VIOLATION_UNREACHABLE_STATE in {v.kind for v in exc.value.violations}


def test_cycle_injected_at_storage_level_is_rejected(repo) -> None:
    service = IndexService(repo)
    _build_persisted(service)

    # 找到一个终结叶子，给它加一条回到根 0 的边，制造环。
    conn = sqlite3.connect(str(repo.path))
    try:
        leaf = conn.execute(
            "SELECT state_id FROM states WHERE is_final=1 "
            "AND state_id NOT IN (SELECT source FROM edges) LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO edges (source, symbol, target) VALUES (?, '~', 0)",
            (leaf,),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(IndexIntegrityError) as exc:
        repo.load_dawg()
    assert VIOLATION_CYCLE in {v.kind for v in exc.value.violations}


def test_corrupted_word_count_is_rejected(repo) -> None:
    service = IndexService(repo)
    _build_persisted(service)

    conn = sqlite3.connect(str(repo.path))
    try:
        conn.execute("UPDATE states SET word_count = word_count + 100 WHERE state_id = 0")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(IndexIntegrityError) as exc:
        repo.load_dawg()
    assert "stored_word_count_mismatch" in {v.kind for v in exc.value.violations}


def test_empty_word_index_roundtrip(tmp_path) -> None:
    repo = SQLiteIndexRepository(tmp_path / "empty_word.sqlite3")
    service = IndexService(repo)
    dawg, _ = service.build_from_words(
        ["", "a", "ab"],
        index_name="with_empty",
        spec=CorpusSpec(allow_empty_word=True),
    )
    assert dawg.states[0].final is True
    loaded = service.load()
    assert loaded.metadata.allow_empty_word is True
    assert loaded.dawg.contains("") is True
    assert loaded.dawg.prefix_count("") == 3


def test_raw_tables_for_diagnostics(repo) -> None:
    service = IndexService(repo)
    _build_persisted(service)
    tables = repo.raw_tables_for_diagnostics()
    assert {t["key"] for t in tables["index_metadata"]} >= {
        "index_name",
        "schema_version",
        "word_count",
    }
    assert len(tables["states"]) == 10
    assert {e["symbol"] for e in tables["edges"]}


def test_repository_exists_flag(repo) -> None:
    assert repo.exists() is False
    service = IndexService(repo)
    _build_persisted(service)
    assert repo.exists() is True
