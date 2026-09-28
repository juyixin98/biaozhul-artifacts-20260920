"""Service-layer tests: answers, proof trees, typed failures, persistence."""

import pytest

from app.service import DatalogService
from app.store.sqlite_store import EvidenceStore

from .fixtures_programs import (
    ANCESTOR_GOAL_ANN,
    ANCESTOR_ANN_BINDINGS,
    ANCESTOR_PROGRAM,
    ANCESTOR_PROGRAM_REORDERED,
    GRAPH_PROGRAM,
    INVALID_PROGRAMS,
    PARSE_INVALID,
)


@pytest.fixture
def service():
    return DatalogService(EvidenceStore(":memory:"))


def test_query_returns_named_bindings_and_proof(service):
    resp = service.run_query(ANCESTOR_PROGRAM, ANCESTOR_GOAL_ANN, request_id="r-anc")
    assert resp.status == "ok"
    assert resp.request_id == "r-anc"
    assert resp.version and resp.version.startswith("sha256:")
    bindings = [a.bindings for a in resp.answers]
    assert bindings == ANCESTOR_ANN_BINDINGS  # exact, ordered
    proof = resp.answers[0].proof
    assert proof["kind"] in ("rule",)
    assert proof["children"], "at least one verifiable derivation step"


def test_every_answer_carries_verifiable_proof_tree(service):
    resp = service.run_query(ANCESTOR_PROGRAM, "ancestor(eve, dan)")
    assert resp.status == "ok"
    assert len(resp.answers) == 1
    proof = resp.answers[0].proof

    def all_leaves_are_facts(node):
        kids = node["children"]
        if not kids:
            return node["kind"] == "fact"
        return all(all_leaves_are_facts(c) for c in kids)

    assert all_leaves_are_facts(proof)
    # stratum metadata is attached
    assert proof["stratum"] == 0


def test_ground_goal_filters_relation(service):
    resp = service.run_query(ANCESTOR_PROGRAM, "ancestor(ann, bob)")
    assert resp.status == "ok" and len(resp.answers) == 1
    resp2 = service.run_query(ANCESTOR_PROGRAM, "ancestor(ann, zzz)")
    assert resp2.status == "empty" and resp2.answers == []


def test_negation_query_explains_absence(service):
    resp = service.run_query(GRAPH_PROGRAM, "sink(X)")
    assert [a.bindings for a in resp.answers] == [{"X": "d"}]
    proof = resp.answers[0].proof
    absence = [c for c in _walk(proof) if c["kind"] == "absence"]
    assert len(absence) == 1
    assert absence[0]["atom"] == "edge(d, *)"


def _walk(node):
    out = [node]
    for c in node["children"]:
        out.extend(_walk(c))
    return out


def test_rule_order_gives_same_answers(service):
    r1 = service.run_query(ANCESTOR_PROGRAM, "ancestor(X, dan)")
    r2 = service.run_query(ANCESTOR_PROGRAM_REORDERED, "ancestor(X, dan)")
    b1 = sorted(a.bindings["X"] for a in r1.answers)
    b2 = sorted(a.bindings["X"] for a in r2.answers)
    assert b1 == b2 == ["ann", "bob", "cy", "eve"]


def test_unsafe_program_reports_failure_category(service):
    resp = service.run_query(INVALID_PROGRAMS["unsafe_head"], "p(X)")
    assert resp.status == "error"
    assert resp.error_category == "unsafe_variable"
    assert resp.to_dict()["failures"][0]["category"] == "unsafe_variable"


def test_negation_cycle_reports_failure_category(service):
    resp = service.run_query(INVALID_PROGRAMS["negation_cycle"], "p(X)")
    assert resp.error_category == "negation_cycle"


@pytest.mark.parametrize("src", PARSE_INVALID)
def test_parse_failures_are_typed(service, src):
    resp = service.run_query(src, "p(a)")
    assert resp.error_category == "parse_error"


def test_unknown_predicate_is_typed(service):
    resp = service.run_query(ANCESTOR_PROGRAM, "nonexistent(X)")
    assert resp.error_category == "unknown_predicate"


def test_steps_and_strata_are_explained(service):
    resp = service.run_query(ANCESTOR_PROGRAM, "ancestor(ann, X)")
    assert resp.strata and resp.strata[0]["iterations"] >= 2
    assert any("stratum 0" in s for s in resp.steps)


def test_persistence_records_request_and_derivations(service):
    resp = service.run_query(ANCESTOR_PROGRAM, "ancestor(ann, X)", request_id="r-persist")
    row = service.store.get_request("r-persist")
    assert row["status"] == "ok"
    assert row["answer_count"] == 4
    summary = service.store.request_summary("r-persist")
    preds = {r["predicate"] for r in summary["relations"]}
    assert {"ancestor/2", "parent/2"} <= preds
    # One derived tuple can be fetched with its SQL support rows.
    deriv = service.store.get_derivation("r-persist", "ancestor", ("ann", "cy"))
    assert deriv is not None and deriv["support"]


def test_failed_request_is_also_persisted(service):
    resp = service.run_query(INVALID_PROGRAMS["unsafe_head"], "p(X)", request_id="r-fail")
    assert resp.status == "error"
    row = service.store.get_request("r-fail")
    assert row["status"] == "error" and "unsafe_variable" in row["message"]


def test_request_ids_are_generated_when_absent(service):
    resp = service.run_query(ANCESTOR_PROGRAM, "ancestor(ann, bob)")
    assert resp.request_id.startswith("req_")


def test_compile_only_reports_strata_without_goal(service):
    resp = service.compile_only(GRAPH_PROGRAM, request_id="r-co")
    assert resp.status == "ok"
    assert resp.request_id == "r-co"
    assert len(resp.strata) == 2


def test_compile_only_typed_failure(service):
    resp = service.compile_only(INVALID_PROGRAMS["negation_cycle"])
    assert resp.status == "error"
    assert resp.error_category == "negation_cycle"


def test_materialize_persists_full_closure(service):
    resp = service.materialize(ANCESTOR_PROGRAM, request_id="r-mat")
    assert resp.status == "ok"
    row = service.store.get_request("r-mat")
    assert row["kind"] == "materialize"
    assert row["answer_count"] == 17  # 12 ancestor + 5 parent
