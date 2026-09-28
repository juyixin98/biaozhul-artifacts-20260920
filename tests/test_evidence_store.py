"""Evidence store: SQLite append-only audit trail tests."""

from __future__ import annotations

from reteapp.config import load_settings
from reteapp.service import SessionManager
from reteapp.storage.evidence import EvidenceStore
from tests.helpers import load_fixture


def _make_session(tmp_path):
    settings = load_settings(db_path=str(tmp_path / "evidence.db"))
    manager = SessionManager()
    session = manager.create(
        load_fixture("rules_orders.json"), settings, run_id="ev-1"
    )
    return session


def test_run_header_rules_and_fact_audit_rows(tmp_path) -> None:
    session = _make_session(tmp_path)
    session.insert({"type": "Customer", "fields": {"id": "c1", "tier": "gold", "city": "NYC"}})
    session.insert({"type": "Order", "fields": {"customer": "c1", "amount": 250}})
    session.retract(2)

    header = session.store.get_run("ev-1")
    assert header is not None
    assert header["version"]
    assert header["settings"]["db_path"].endswith("evidence.db")

    # Rules compiled into the store with structured plans.
    conn = session.store._conn
    names = [r["rule_name"] for r in conn.execute(
        "SELECT rule_name FROM rules WHERE run_id = 'ev-1' ORDER BY seq"
    ).fetchall()]
    assert names == ["gold_customer_order", "customer_order_any_tier", "same_city_customer_pair"]

    # Append-only fact audit: both inserts and the retract are present.
    ops = [(r["wme_id"], r["op"]) for r in conn.execute(
        "SELECT wme_id, op FROM facts WHERE run_id = 'ev-1' ORDER BY id"
    ).fetchall()]
    assert ops == [(1, "insert"), (2, "insert"), (2, "retract")]


def test_activations_firings_and_invalidation_are_audited(tmp_path) -> None:
    session = _make_session(tmp_path)
    session.insert({"type": "Customer", "fields": {"id": "c1", "tier": "gold", "city": "NYC"}})
    session.insert({"type": "Order", "fields": {"customer": "c1", "amount": 250}})
    report = session.fire("all")
    assert report.status == "fired"

    conn = session.store._conn
    phases = {r["phase"] for r in conn.execute(
        "SELECT DISTINCT phase FROM activations WHERE run_id = 'ev-1'"
    ).fetchall()}
    assert "queued" in phases and "fired" in phases
    firings = conn.execute(
        "SELECT rule_name, asserted, retracted FROM firings WHERE run_id = 'ev-1' "
        "ORDER BY fire_order"
    ).fetchall()
    assert [f["rule_name"] for f in firings] == ["gold_customer_order", "customer_order_any_tier"]
    assert firings[0]["asserted"] == "[3]"

    # Retract invalidation leaves an explicit audit row (not silent removal).
    session.retract(1)
    invalidated = conn.execute(
        "SELECT COUNT(*) AS c FROM activations WHERE run_id = 'ev-1' AND phase = 'invalidated'"
    ).fetchone()["c"]
    assert invalidated >= 1


def test_trace_is_ordered_and_correlatable_with_version_and_run(tmp_path) -> None:
    session = _make_session(tmp_path)
    session.insert({"type": "Customer", "fields": {"id": "c1", "tier": "gold", "city": "NYC"}})
    session.insert({"type": "Order", "fields": {"customer": "c1", "amount": 250}})
    trace = session.trace()
    events = [row["event"] for row in trace]
    assert "run_started" in events
    assert "alpha_memory_hit" in events
    assert "activation_queued" in events
    assert "fact_inserted" in events
    # Strict ordering by sequence.
    seqs = [row["seq"] for row in trace]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    # Each row carries run correlation and version-bearing run header.
    assert all(row["run_id"] == "ev-1" for row in trace)


def test_evidence_summary_counts(tmp_path) -> None:
    session = _make_session(tmp_path)
    session.insert({"type": "Customer", "fields": {"id": "c1", "tier": "gold", "city": "NYC"}})
    session.insert({"type": "Order", "fields": {"customer": "c1", "amount": 250}})
    session.fire("all")
    summary = session.evidence()
    assert summary["facts"]["insert"] == 4  # customer, order + 2 asserted
    assert summary["firings"] == 2


def test_persistent_store_survives_reopen(tmp_path) -> None:
    db_path = tmp_path / "persist.db"
    first = EvidenceStore(str(db_path))
    first.start_run("persist-1", {"db_path": str(db_path)}, note="reopen-check")
    first.append_trace(
        "persist-1", 1, "2026-09-28T00:00:00+00:00", "INFO", "marker", {"k": "v"}
    )
    first.close()

    reopened = EvidenceStore(str(db_path))
    try:
        header = reopened.get_run("persist-1")
        assert header is not None and header["note"] == "reopen-check"
        trace = reopened.get_trace("persist-1")
        assert len(trace) == 1 and trace[0]["event"] == "marker"
        assert trace[0]["run_id"] == "persist-1"
        assert reopened.get_run("missing") is None
    finally:
        reopened.close()
