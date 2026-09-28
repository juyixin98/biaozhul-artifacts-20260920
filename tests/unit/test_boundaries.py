"""Additional boundary tests: corpus spec validation, loader, config, store."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from collsvc.config import Settings
from collsvc.corpus.loader import entries_from_texts, load_corpus, load_corpus_bundle
from collsvc.corpus.spec import CorpusSpec, InvalidCorpusSpec
from collsvc.collation.options import CollationOptions
from collsvc.collation.versioning import VersionMismatchError
from collsvc.index.store import IndexNotFoundError, IndexStore
from collsvc.mining.miner import mine


def _write(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_single_and_bundle(tmp_path):
    single = _write(tmp_path / "one.json", {"name": "one", "entries": ["x", "y"]})
    spec = load_corpus(single)
    assert spec.name == "one" and len(spec.entries) == 2

    bundle = _write(tmp_path / "many.json", {
        "corpora": [
            {"name": "a", "entries": ["1"]},
            {"name": "b", "entries": ["2"]},
        ]
    })
    specs = load_corpus_bundle(bundle)
    assert [s.name for s in specs] == ["a", "b"]


def test_bundle_duplicate_names_rejected(tmp_path):
    bundle = _write(tmp_path / "dup.json", {
        "corpora": [{"name": "a", "entries": ["1"]},
                    {"name": "a", "entries": ["2"]}],
    })
    with pytest.raises(InvalidCorpusSpec):
        load_corpus_bundle(bundle)


def test_corpus_name_must_match_file_stem(tmp_path):
    path = _write(tmp_path / "stem.json", {"name": "other", "entries": ["x"]})
    with pytest.raises(InvalidCorpusSpec):
        load_corpus(path)


@pytest.mark.parametrize("payload", [
    {"entries": ["x"]},                      # missing name
    {"name": "ok"},                          # missing entries
    {"name": "ok", "entries": []},           # empty
    {"name": "bad name", "entries": ["x"]},  # invalid name
    {"name": "ok", "entries": [123]},        # bad entry shape
    {"name": "ok", "entries": [{"no_text": 1}]},
])
def test_invalid_spec_shapes(payload):
    with pytest.raises(InvalidCorpusSpec):
        CorpusSpec.from_dict(payload)


def test_string_entries_get_auto_doc_ids():
    spec = CorpusSpec.from_dict({"name": "auto", "entries": ["a", "b"]})
    assert [e.doc_id for e in spec.entries] == ["auto-0000", "auto-0001"]


def test_missing_corpus_file_is_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_corpus(tmp_path / "nope.json")


def test_entries_from_texts_helper():
    spec = entries_from_texts("t", ["only"])
    assert spec.name == "t"
    assert spec.entries[0].doc_id == "t-0000"


def test_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLSVC_PAGE_SIZE_DEFAULT", "7")
    monkeypatch.setenv("COLLSVC_PAGE_SIZE_MAX", "99")
    monkeypatch.setenv("COLLSVC_DB_PATH", str(tmp_path / "x.db"))
    settings = Settings.from_env()
    assert settings.page_size_default == 7
    assert settings.page_size_max == 99
    assert settings.db_path == tmp_path / "x.db"


def test_store_open_without_build_raises(tmp_path):
    store = IndexStore(tmp_path / "empty.db", CollationOptions())
    with pytest.raises(IndexNotFoundError):
        store.require_open()
    assert store.is_built is False
    store.close()


def test_build_twice_with_same_version_is_unchanged(tmp_path):
    spec = CorpusSpec.from_dict({"name": "twice", "entries": ["a", "b"]})
    store = IndexStore(tmp_path / "t.db", CollationOptions())
    first = store.build(spec)
    second = store.build(spec)
    assert first["status"] == "built"
    assert second["status"] == "unchanged"
    assert first["index_version"] == second["index_version"]


def test_build_without_replace_refuses_version_change(tmp_path):
    spec = CorpusSpec.from_dict({"name": "x", "entries": ["a"]})
    db = tmp_path / "x.db"
    IndexStore(db, CollationOptions(numeric=False)).build(spec)
    changed = IndexStore(db, CollationOptions(numeric=True))
    with pytest.raises(VersionMismatchError):
        changed.build(spec, replace=False)


def test_count_range_and_total(tmp_path):
    spec = CorpusSpec.from_dict({"name": "c", "entries": ["a", "m", "z"]})
    store = IndexStore(tmp_path / "c.db", CollationOptions())
    store.build(spec)
    assert store.total_count() == 3
    lo, hi = store.key_for("a"), store.key_for("m")
    assert store.count_range(lo, hi) == 2
