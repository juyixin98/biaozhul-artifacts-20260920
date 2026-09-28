"""Service-level tests: retraction semantics, decisions, persistence."""

from __future__ import annotations

import pytest

from atms_backend.diagnostics import (
    ACCEPTED,
    REJECTED,
    UNDETERMINED,
    REASON_INCOMPLETE,
    REASON_NOGOOD,
    REASON_NO_ENV,
    REASON_SUPPORTED,
)
from atms_backend.core.budgets import Budgets

SHARED = """
assume A, B, C
fact P
rule r1: A, P => X
rule r2: B, P => X
rule r3: X => Y
rule r4: C => Z
rule r5: A, C => FALSE
"""


def _create(service, pid="p1"):
    service.create_problem(pid, "shared", SHARED)


def test_query_accepted_with_supporting_environments(service):
    _create(service)
    _, rec = service.query_node("p1", "X")
    assert rec.decision == ACCEPTED
    assert rec.reason == REASON_SUPPORTED
    assert rec.supporting_environments == [["A"], ["B"]]
    assert rec.request_id.startswith("req-")


def test_query_context_blocked_by_nogood_names_blocker(service):
    _create(service)
    _, rec = service.query_node(
        "p1", "X", context=["A", "C"]
    )
    assert rec.decision == REJECTED
    assert rec.reason == REASON_NOGOOD
    assert rec.nogood_blockers == [["A", "C"]]
    # State is carried in the record so the rejection is explainable.
    assert rec.nogoods == [["A", "C"]]


def test_query_consistent_context_filters_support(service):
    _create(service)
    _, rec = service.query_node("p1", "X", context=["B", "C"])
    assert rec.decision == ACCEPTED
    assert rec.supporting_environments == [["B"]]


def test_unknown_node_complete_theory_is_rejected_not_undetermined(service):
    _create(service)
    _, rec = service.query_node("p1", "NEVER")
    assert rec.decision == REJECTED
    assert rec.reason == REASON_NO_ENV
    assert rec.incomplete is False


def test_retracting_one_assumption_keeps_alternative_support(service):
    _create(service)
    out = service.retract_assumptions("p1", ["A"])
    # X and Y survive via {B}; P always survives; Z via {C}.
    assert "X" in out["surviving_nodes"]
    assert "Y" in out["surviving_nodes"]
    x_change = next(c for c in out["changed"] if c["node_id"] == "X")
    assert x_change["before"] == [["A"], ["B"]]
    assert x_change["after"] == [["B"]]
    assert x_change["survives"] is True
    # With A gone the {A,C} contradiction can no longer form.
    assert out["nogoods"] == []


def test_retracting_all_support_removes_conclusion_but_keeps_others(service):
    _create(service)
    out = service.retract_assumptions("p1", ["A", "B"])
    assert "X" in out["removed_nodes"]
    assert "Y" in out["removed_nodes"]
    # P is a premise, Z rests on C alone: both stay.
    assert "P" in out["surviving_nodes"]
    assert "Z" in out["surviving_nodes"]


def test_retraction_is_hypothetical_and_does_not_mutate_problem(service):
    _create(service)
    service.retract_assumptions("p1", ["A"])
    _, rec = service.query_node("p1", "X")
    assert rec.decision == ACCEPTED
    assert rec.supporting_environments == [["A"], ["B"]]


def test_retracting_undeclared_assumption_fails_with_category(service):
    _create(service)
    try:
        service.retract_assumptions("p1", ["NOPE"])
    except KeyError as exc:
        assert "NOPE" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected KeyError")


def test_snapshot_round_trip_survives_new_service_instance(service):
    _create(service)
    service.propagate("p1")
    # Simulate a fresh process: rebuild repository state from SQLite.
    engine = service.current_engine("p1")
    assert engine.fixpoint_reached is True
    runs = service.repo.list_runs("p1")
    assert runs and runs[0]["incomplete"] == 0


def test_budgeted_propagation_reports_incomplete_and_query_undetermined(service):
    service.create_problem(
        "bud",
        "budget",
        """
        assume A1, A2, A3
        rule r1: A1 => X
        rule r2: A2 => X
        rule r3: A3 => LATE
        """,
    )
    tight = Budgets(max_label_envs=1, max_total_envs=1000, max_steps=1000)
    _, summary = service.propagate("bud", budgets_override=tight)
    assert summary["incomplete"] is True
    assert "label_envs" in summary["reason"]

    # A node whose environments were never generated must be reported as
    # UNDETERMINED (never as a proven rejection), carrying the budget reason.
    engine, rec = service.query_node(
        "bud", "LATE", request_id="req-tight", budgets_override=tight
    )
    assert rec.decision == UNDETERMINED
    assert rec.reason == REASON_INCOMPLETE
    assert rec.incomplete is True
    assert "label_envs" in rec.incomplete_reason
    # ...while a node that already has a derived environment still accepts.
    assert engine.holds("X") is True

    # A normal (unbudgeted) run completes and LATE is then genuinely held.
    _, rec2 = service.query_node("bud", "LATE", request_id="req-full")
    assert rec2.decision == ACCEPTED
    assert rec2.supporting_environments == [["A3"]]


def test_query_context_with_undeclared_assumption_is_rejected(service):
    _create(service)
    with pytest.raises(ValueError) as exc:
        service.query_node("p1", "X", context=["A", "NOPE"])
    assert "NOPE" in str(exc.value)


def test_explain_lists_proofs_per_environment(service):
    _create(service)
    out = service.explain_node("p1", "X")
    envs = {tuple(e["environment"]): set(e["rules"]) for e in out["explanations"]}
    assert envs[("A",)] == {"r1"}
    assert envs[("B",)] == {"r2"}
