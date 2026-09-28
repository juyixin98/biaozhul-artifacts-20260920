"""Contracts against the checked-in sample fixtures (hand-computed checks)."""

from __future__ import annotations

from atms_backend.core.budgets import Budgets


def test_shared_fixture_hand_labels_via_service(service, fixture_source):
    service.create_problem("shared", "shared", fixture_source("shared_reasoning.atms"))
    _, x = service.query_node("shared", "X")
    assert x.supporting_environments == [["A"], ["B"]]
    blocked = service.query_node("shared", "X", context=["A", "C"])[1]
    assert blocked.decision == "rejected"
    assert blocked.nogood_blockers == [["A", "C"]]


def test_diamond_fixture_full_context_contradiction(service, fixture_source):
    service.create_problem("diamond", "diamond", fixture_source("diamond.atms"))
    assert service.nogoods("diamond")["nogoods"] == [["A", "B"]]
    rec = service.query_node(
        "diamond", "Q", context=["A", "B", "D"]
    )[1]
    assert rec.decision == "rejected"
    assert rec.reason == "blocked_by_nogood"


def test_budget_fixture_incompleteness_categories(service, fixture_source):
    service.create_problem("budget", "budget", fixture_source("budget_ten.atms"))
    tight = Budgets(max_label_envs=4, max_total_envs=100_000, max_steps=100_000)

    # Tight run: X is partially derived; L9's env is never generated.
    engine, x = service.query_node(
        "budget", "X", budgets_override=tight, request_id="req-bx"
    )
    assert x.decision == "accepted"
    assert x.incomplete is True
    assert len(x.supporting_environments) == 4

    _, l9 = service.query_node(
        "budget", "L9", budgets_override=tight, request_id="req-bl9"
    )
    assert l9.decision == "undetermined"
    assert l9.reason == "propagation_incomplete"
    assert "label_envs" in l9.incomplete_reason

    # Normal run: complete label, L9 genuinely supported by {A9}.
    _, l9_full = service.query_node("budget", "L9", request_id="req-full")
    assert l9_full.decision == "accepted"
    assert l9_full.supporting_environments == [["A9"]]


def test_budget_override_is_not_softened_by_completed_snapshot(
    service, fixture_source
):
    # Regression: a tight override must run under its OWN budgets even when
    # a complete fixpoint snapshot already exists.
    service.create_problem("budget", "budget", fixture_source("budget_ten.atms"))
    _, full = service.propagate("budget", request_id="req-complete")
    assert full["incomplete"] is False
    assert len(full["labels"]["X"]) == 10

    tight = Budgets(max_label_envs=4, max_total_envs=100_000, max_steps=100_000)
    _, again = service.propagate(
        "budget", request_id="req-tight", budgets_override=tight
    )
    assert again["incomplete"] is True
    assert len(again["labels"]["X"]) == 4
    # The complete snapshot must remain the served truth afterwards.
    engine = service.current_engine("budget")
    assert engine.fixpoint_reached is True
    assert len(engine.holding_environments("X")) == 10
