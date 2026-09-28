"""Evidence-store tests: persistence, content deduplication, audit log."""

import pytest

pytestmark = pytest.mark.unit

from datalog_service.engine.fixpoint import evaluate
from datalog_service.language.compiler import compile_program
from datalog_service.language.parser import parse_program
from datalog_service.storage.evidence_store import EvidenceStore, json_to_row, row_to_json

SOURCE = """
parent(a, b). parent(b, c).
anc(X, Y) :- parent(X, Y).
anc(X, Y) :- parent(X, Z), anc(Z, Y).
"""


def _prepared():
    program = parse_program(SOURCE)
    compiled = compile_program(program)
    return program, compiled, evaluate(compiled, program.facts)


def test_program_and_materialization_round_trip():
    store = EvidenceStore(":memory:")
    program, compiled, mat = _prepared()
    assert store.save_program("p1", compiled, program.facts, mat.fact_set_version)
    assert store.save_materialization(mat, "p1")

    record = store.get_program("p1")
    assert record["rule_version"] == compiled.rule_version
    assert record["rule_count"] == 2
    assert record["fact_count"] == 2

    mat_record = store.get_materialization_record(mat.materialization_id)
    assert mat_record["fact_set_version"] == mat.fact_set_version
    store.close()


def test_same_content_is_deduplicated():
    store = EvidenceStore(":memory:")
    program, compiled, mat = _prepared()
    assert store.save_program("p1", compiled, program.facts, mat.fact_set_version)
    assert store.save_program("p2", compiled, program.facts, mat.fact_set_version) is False
    assert store.find_program(compiled.rule_version, mat.fact_set_version) == "p1"
    store.close()


def test_tuple_rows_record_derivation_metadata():
    store = EvidenceStore(":memory:")
    program, compiled, mat = _prepared()
    store.save_program("p1", compiled, program.facts, mat.fact_set_version)
    store.save_materialization(mat, "p1")
    rows = store.list_tuple_rows(mat.materialization_id, "anc")
    assert len(rows) == 3  # two length-1 pairs plus (a,c)
    derived = [r for r in rows if r["kind"] == "derived"]
    by_row = {tuple(json_to_row(r["row_json"])): r for r in derived}
    ac = by_row[("a", "c")]
    assert ac["rule_id"] == "r2"
    assert ac["round"] == 1
    store.close()


def test_row_json_round_trips_integers_and_symbols():
    assert json_to_row(row_to_json(("a", 7, "b"))) == ("a", 7, "b")


def test_request_log_correlates_request_ids():
    store = EvidenceStore(":memory:")
    store.log_request(
        request_id="req-42", endpoint="POST /programs", status="ok",
        http_status=200, program_id="p1", result_count=2,
        details={"note": "explainable"},
    )
    record = store.get_request("req-42")
    assert record["endpoint"] == "POST /programs"
    assert record["details"] == {"note": "explainable"}
    assert store.get_request("missing") is None
    store.close()


def test_persistent_store_survives_reopen(tmp_path):
    db_path = str(tmp_path / "evidence" / "store.db")
    store = EvidenceStore(db_path)
    program, compiled, mat = _prepared()
    store.save_program("p1", compiled, program.facts, mat.fact_set_version)
    store.save_materialization(mat, "p1")
    store.close()

    reopened = EvidenceStore(db_path)
    assert reopened.get_program("p1")["rule_version"] == compiled.rule_version
    assert len(reopened.list_tuple_rows(mat.materialization_id, "anc")) == 3
    reopened.close()
