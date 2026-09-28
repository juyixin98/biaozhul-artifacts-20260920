"""Direct unit tests for the SQLite evidence store."""

from __future__ import annotations

from min_iowl.store.evidence import EvidenceStore


def _save(store: EvidenceStore, oid: str = "o1") -> None:
    store.save_ontology(
        oid,
        "label",
        [
            ("a001", 0, "SubClassOf", "SubClassOf(A B)", {"kind": "SubClassOf"}),
            ("a002", 1, "ClassAssertion", "ClassAssertion(A x)", {"kind": "ClassAssertion"}),
        ],
    )


def test_ontology_lifecycle(tmp_path) -> None:
    store = EvidenceStore(str(tmp_path / "e.sqlite3"))
    assert store.ontology_exists("o1") is False
    _save(store)
    assert store.ontology_exists("o1") is True

    listings = store.list_ontologies()
    assert len(listings) == 1
    assert listings[0]["ontology_id"] == "o1"
    assert listings[0]["axiom_count"] == 2

    rows = store.load_axioms("o1")
    assert [r.axiom_id for r in rows] == ["a001", "a002"]
    assert rows[0].functional_text == "SubClassOf(A B)"


def test_run_persistence_and_reload(tmp_path) -> None:
    store = EvidenceStore(str(tmp_path / "e.sqlite3"))
    _save(store)
    store.save_run("run-1", "o1", "engine/9", 1.5, True, ["C"], {"state": {}})
    runs = store.list_runs("o1")
    assert len(runs) == 1
    assert runs[0]["run_id"] == "run-1"
    assert runs[0]["inconsistent"] is True
    assert runs[0]["unsatisfiable"] == ["C"]
    assert store.load_run_report("run-1") == {"state": {}}
    assert store.load_run_report("missing") is None


def test_request_event_log_roundtrip(tmp_path) -> None:
    store = EvidenceStore(str(tmp_path / "e.sqlite3"))
    store.log_request("req-9", "POST", "/x", "received")
    store.log_request("req-9", "POST", "/x", "completed", 200, {"ms": 2})
    events = store.request_events("req-9")
    assert [e["stage"] for e in events] == ["received", "completed"]
    assert events[1]["status_code"] == 200
    assert events[1]["detail"] == {"ms": 2}
    assert store.request_events("unknown") == []


def test_store_persists_across_connections(tmp_path) -> None:
    path = str(tmp_path / "e.sqlite3")
    _save(EvidenceStore(path))
    # a brand-new store instance over the same file sees committed data
    assert EvidenceStore(path).ontology_exists("o1") is True
