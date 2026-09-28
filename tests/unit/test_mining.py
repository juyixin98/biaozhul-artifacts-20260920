"""Mining kernel + corpus spec tests."""
from __future__ import annotations

import unicodedata

import pytest

from collsvc.corpus.loader import entries_from_texts, load_corpus_bundle
from collsvc.corpus.spec import CorpusEntry, CorpusSpec, InvalidCorpusSpec
from collsvc.mining.miner import mine

DEMO = __import__("pathlib").Path(__file__).resolve().parents[2] / "data" / "corpus" / "demo.json"


def test_demo_corpus_loads_and_mines():
    bundle = load_corpus_bundle(DEMO)
    spec = bundle[0]
    assert spec.name == "demo"
    report = mine(spec)
    assert report.stats["entry_count"] == 35
    # Several multi-spelling NFC/NFD equivalence classes.
    multi = [c for c in report.equivalence_classes if c.identity_preserved]
    assert len(multi) >= 4
    # And the duplicate raw-text group (two doc ids, identical bytes).
    assert any(len(g) == 2 for g in report.duplicate_text_groups)


def test_equivalence_class_keeps_distinct_identities():
    nfc = "café"
    nfd = unicodedata.normalize("NFD", nfc)
    spec = CorpusSpec.from_dict({
        "name": "eq",
        "entries": [
            {"doc_id": "x1", "text": nfc},
            {"doc_id": "x2", "text": nfd},
        ],
    })
    report = mine(spec)
    cls = next(c for c in report.equivalence_classes if c.nfc_text == nfc)
    assert cls.identity_preserved is True
    assert cls.doc_ids == ("x1", "x2")
    assert set(cls.distinct_raw_texts) == {nfc, nfd}


def test_duplicate_doc_ids_rejected():
    with pytest.raises(InvalidCorpusSpec):
        CorpusSpec.from_dict({
            "name": "dup",
            "entries": [
                {"doc_id": "a", "text": "x"},
                {"doc_id": "a", "text": "y"},
            ],
        })


def test_non_string_text_rejected():
    with pytest.raises(InvalidCorpusSpec):
        CorpusEntry(doc_id="a", text=123)  # type: ignore[arg-type]


def test_probes_cover_required_categories():
    spec = load_corpus_bundle(DEMO)[0]
    categories = {p.category for p in mine(spec).probes}
    assert {"accent", "numeric", "case", "turkish", "canonical_equivalence"} <= categories


def test_entries_from_texts_helper():
    spec = entries_from_texts("adhoc", ["a", "b"])
    assert [e.doc_id for e in spec.entries] == ["t-0000", "t-0001"]
