"""Unit tests for the bounded HTN kernel.

These build small domains inline (not reusing the fixture domains) so each
semantic property is isolated.
"""

from __future__ import annotations

import pytest

from htn_planner.core.engine import Bounds, Planner, PlanningError
from htn_planner.lang import parse_domain, parse_problem


def solve(domain_text: str, problem_text: str, **bounds: object):
    planner = Planner(Bounds(**{k: v for k, v in bounds.items()}))  # type: ignore[arg-type]
    return planner.solve(
        parse_domain(domain_text), parse_problem(problem_text), "req-test"
    )


# --------------------------------------------------------------------------- #
# Primitive execution / preconditions
# --------------------------------------------------------------------------- #

TOGGLE_DOMAIN = """
(:domain toggle
  (:operator (!on) (:pre (off)) (:del (off)) (:add (on)))
  (:operator (!broken) (:pre (never)) (:del) (:add (x)))
  (:method m-go (t) (:pre) (:tasks (!on)))
  (:method m-bad (t) (:pre) (:tasks (!broken))))
"""


class TestPrimitiveExecution:
    def test_precondition_true_allows_execution_and_applies_effects(self) -> None:
        result = solve(TOGGLE_DOMAIN, "(:problem p (:domain toggle) (:init (off)) (:tasks (!on)))")
        assert result.status == "success"
        assert [a.operator for a in result.plan] == ["!on"]
        assert result.plan[0].added == (("on",),)
        assert result.plan[0].deleted == (("off",),)

    def test_action_with_false_precondition_is_a_deadlock(self) -> None:
        result = solve(TOGGLE_DOMAIN, "(:problem p (:domain toggle) (:init) (:tasks (!broken)))")
        assert result.status == "failure"
        assert {f.category for f in result.failures} == {"deadlock"}
        unmet = result.failures[0].blocked[0]["unmet"]
        assert "(never)" in unmet

    def test_delete_then_add_semantics(self) -> None:
        domain = """
        (:domain d
          (:operator (!swap ?x ?y)
            (:pre (a ?x))
            (:del (a ?x))
            (:add (a ?y))))
        """
        problem = "(:problem p (:domain d) (:init (a 1)) (:tasks (!swap 1 2)))"
        result = solve(domain, problem)
        assert result.status == "success"
        assert ("a", 1) not in set(_final_state(result))
        # (a 2) present: delete then add cannot delete a freshly added fact
        # via the same operator unless explicitly listed.


def _final_state(result: object) -> list[tuple[object, ...]]:
    # Reconstructed from action effects in test helper.
    state: set[tuple[object, ...]] = set()
    for step in result.plan:  # type: ignore[attr-defined]
        state -= set(step.deleted)
        state |= set(step.added)
    return list(state)


# --------------------------------------------------------------------------- #
# Method selection / mutual exclusion
# --------------------------------------------------------------------------- #

CHOOSE_DOMAIN = """
(:domain choose
  (:operator (!a) (:pre (mode-a)) (:del (mode-a)) (:add (done-a)))
  (:operator (!b) (:pre (mode-b)) (:del (mode-b)) (:add (done-b)))
  (:method ma (pick) (:pre (mode-a)) (:tasks (!a)))
  (:method mb (pick) (:pre (mode-b)) (:tasks (!b))))
"""


class TestMethodSelection:
    @pytest.mark.parametrize(
        "init,expected_action",
        [(["(mode-a)"], "!a"), (["(mode-b)"], "!b")],
    )
    def test_selects_applicable_method(self, init: list[str], expected_action: str) -> None:
        problem = f"(:problem p (:domain choose) (:init {' '.join(init)}) (:tasks (pick)))"
        result = solve(CHOOSE_DOMAIN, problem)
        assert result.status == "success"
        assert result.plan[0].operator == expected_action

    def test_no_applicable_method_lists_every_rejection(self) -> None:
        problem = "(:problem p (:domain choose) (:init (mode-c)) (:tasks (pick)))"
        result = solve(CHOOSE_DOMAIN, problem)
        assert result.status == "failure"
        record = result.failures[0]
        assert record.category == "no_applicable_method"
        assert [m["method"] for m in record.rejected_methods] == ["ma", "mb"]
        assert record.rejected_methods[0]["failure_kind"] == "no_matching_fact"

    def test_declaration_order_drives_backtracking(self) -> None:
        # First method is applicable but its single action !a cannot run in
        # this state (mode-a absent), so the engine must backtrack; mb also
        # fails -> failure, proving both branches were explored.
        domain = """
        (:domain d
          (:operator (!a) (:pre (x)) (:del) (:add (r)))
          (:operator (!b) (:pre (y)) (:del) (:add (r)))
          (:method m1 (t) (:pre) (:tasks (!a)))
          (:method m2 (t) (:pre) (:tasks (!b))))
        """
        result = solve(domain, "(:problem p (:domain d) (:init) (:tasks (t)))")
        assert result.status == "failure"
        assert result.counters["backtracks"] >= 1
        # Both methods were themselves applicable (precondition empty); the
        # dead end came from primitive preconditions, not method rejection.
        assert result.failures[0].category == "deadlock"


# --------------------------------------------------------------------------- #
# Recursion
# --------------------------------------------------------------------------- #

CHAIN_DOMAIN = """
(:domain chain
  (:operator (!take ?x)
    (:pre (avail ?x)) (:del (avail ?x)) (:add (done ?x)))
  (:method m-last (proc ?x)
    (:pre (last ?x)) (:tasks (!take ?x)))
  (:method m-step (proc ?x)
    (:pre (next ?x ?y)) (:tasks (!take ?x) (proc ?y))))
"""


class TestRecursion:
    def test_recursive_chain_decomposes_in_order(self) -> None:
        problem = """
        (:problem p (:domain chain)
          (:init (avail a) (next a b) (avail b) (next b c) (avail c) (last c))
          (:tasks (proc a)))
        """
        result = solve(CHAIN_DOMAIN, problem, max_depth=6)
        assert result.status == "success"
        assert [a.args[0] for a in result.plan] == ["a", "b", "c"]
        assert result.counters["expansions"] == 3

    def test_depth_below_requirement_is_inconclusive_not_failure(self) -> None:
        problem = """
        (:problem p (:domain chain)
          (:init (avail a) (next a b) (avail b) (last b))
          (:tasks (proc a)))
        """
        # Depth 1 allows expanding proc(a) but not the nested proc(b): budget.
        result = solve(CHAIN_DOMAIN, problem, max_depth=0)
        assert result.status == "inconclusive"
        assert {u.category for u in result.uncertain} == {"depth_budget"}
        assert result.failures == ()

    def test_broken_chain_is_definitive_no_method_failure(self) -> None:
        problem = """
        (:problem p (:domain chain)
          (:init (avail a) (next a b) (avail b))
          (:tasks (proc a)))
        """
        result = solve(CHAIN_DOMAIN, problem, max_depth=6)
        assert result.status == "failure"
        assert result.failures[0].category == "no_applicable_method"
        assert result.failures[0].task == ["proc", "b"]


# --------------------------------------------------------------------------- #
# Ordering
# --------------------------------------------------------------------------- #

ORDER_DOMAIN = """
(:domain order
  (:operator (!a) (:pre) (:del) (:add (pa)))
  (:operator (!b) (:pre (pa)) (:del (pa)) (:add (pb)))
  (:operator (!c) (:pre (pb)) (:del (pb)) (:add (pc)))
  (:method m-seq (seq) (:pre) (:tasks (!a) (!b) (!c)))
  (:method m-par (par) (:pre)
    (:tasks (:partial ((!a) (!b) (!c)) (:before 0 1) (:before 1 2))))
  (:method m-free (free) (:pre)
    (:tasks (:partial ((!b) (!a))))))
"""


class TestOrdering:
    def test_ordered_subtasks_enforce_sequence(self) -> None:
        result = solve(ORDER_DOMAIN, "(:problem p (:domain order) (:init) (:tasks (seq)))")
        assert [a.operator for a in result.plan] == ["!a", "!b", "!c"]

    def test_partial_constraints_force_required_order(self) -> None:
        # !b needs (pa) produced by !a; only an a-before-b order works, which
        # the partial constraint guarantees.
        result = solve(ORDER_DOMAIN, "(:problem p (:domain order) (:init) (:tasks (par)))")
        assert [a.operator for a in result.plan] == ["!a", "!b", "!c"]

    def test_unordered_tasks_choose_any_feasible_linearization(self) -> None:
        # !b requires (pa); with !a and !b unordered the planner must discover
        # that !a has to be taken first despite the absence of an edge.
        result = solve(ORDER_DOMAIN, "(:problem p (:domain order) (:init) (:tasks (free)))")
        assert [a.operator for a in result.plan] == ["!a", "!b"]

    def test_partial_dag_that_is_unexecutable_is_deadlock(self) -> None:
        # Two independent tasks both demand a fact nobody establishes.  The
        # network has no ordering edges (a valid DAG), yet nothing can run.
        domain = """
        (:domain d
          (:operator (!x) (:pre (need)) (:del) (:add (rx)))
          (:method m (both) (:pre)
            (:tasks (:partial ((!x) (!x))))))
        """
        result = solve(domain, "(:problem p (:domain d) (:init) (:tasks (both)))")
        assert result.status == "failure"
        assert result.failures[0].category == "deadlock"
        assert len(result.failures[0].remaining_tasks) == 2


# --------------------------------------------------------------------------- #
# Shared resource (the headline semantic)
# --------------------------------------------------------------------------- #

RESOURCE_DOMAIN = """
(:domain res
  (:operator (!take ?r ?w) (:pre (free ?r)) (:del (free ?r)) (:add (held ?r ?w)))
  (:operator (!give ?r ?w) (:pre (held ?r ?w)) (:del (held ?r ?w)) (:add (free ?r)))
  (:method m-job (job ?r ?w) (:pre (worker ?w)) (:tasks (!take ?r ?w) (!give ?r ?w)))
  (:method m-two (two ?r ?a ?b)
    (:pre (worker ?a) (worker ?b))
    (:tasks (:partial ((job ?r ?a) (job ?r ?b))))))
"""


class TestSharedResource:
    def test_plan_serializes_the_two_jobs(self) -> None:
        problem = "(:problem p (:domain res) (:init (free R) (worker a) (worker b)) (:tasks (two R a b)))"
        result = solve(RESOURCE_DOMAIN, problem)
        assert result.status == "success"
        actions = [(x.operator, x.args[1]) for x in result.plan]
        assert actions == [
            ("!take", "a"), ("!give", "a"),
            ("!take", "b"), ("!give", "b"),
        ] or actions == [
            ("!take", "b"), ("!give", "b"),
            ("!take", "a"), ("!give", "a"),
        ]

    def test_non_released_resource_deadlocks_in_every_branch(self) -> None:
        domain = """
        (:domain d
          (:operator (!g ?r ?w) (:pre (free ?r)) (:del (free ?r)) (:add (h ?r ?w)))
          (:method m1 (c ?r ?w) (:pre) (:tasks (!g ?r ?w)))
          (:method m2 (both ?r ?a ?b) (:pre)
            (:tasks (:partial ((c ?r ?a) (c ?r ?b))))))
        """
        result = solve(
            domain,
            "(:problem p (:domain d) (:init (free R)) (:tasks (both R a b)))",
        )
        assert result.status == "failure"
        assert all(f.category == "deadlock" for f in result.failures)


# --------------------------------------------------------------------------- #
# Bounds and cycles
# --------------------------------------------------------------------------- #

BOUNDS_DOMAIN = """
(:domain b
  (:operator (!tick ?x) (:pre (at ?x)) (:del (at ?x)) (:add (done ?x)))
  (:operator (!hop ?x ?y) (:pre (at ?x) (edge ?x ?y)) (:del (at ?x)) (:add (at ?y)))
  (:method mb (walk ?x) (:pre (target ?x)) (:tasks (!tick ?x)))
  (:method ms (walk ?x) (:pre (edge ?x ?y) (not (target ?x)))
    (:tasks (!hop ?x ?y) (walk ?y)))
  (:method mspin (spin ?x) (:pre (spinning ?x)) (:tasks (spin ?x))))
"""


class TestBoundsAndCycles:
    def test_nonprogress_cycle_is_definitive_failure(self) -> None:
        result = solve(
            BOUNDS_DOMAIN,
            "(:problem p (:domain b) (:init (spinning z)) (:tasks (spin z)))",
            max_depth=5,
        )
        assert result.status == "failure"
        rec = result.failures[0]
        assert rec.category == "cycle"
        chain = [tuple(t) for t in rec.cycle_chain]
        assert chain[0] == chain[-1] == ("spin", "z")

    def test_expansion_budget_cut_is_inconclusive(self) -> None:
        problem = """
        (:problem p (:domain b)
          (:init (at a) (edge a b) (edge b c) (target c))
          (:tasks (walk a)))
        """
        result = solve(BOUNDS_DOMAIN, problem, max_depth=10, max_expansions=1)
        assert result.status == "inconclusive"
        assert {u.category for u in result.uncertain} == {"expansion_budget"}

    def test_action_horizon_cut_is_inconclusive(self) -> None:
        problem = """
        (:problem p (:domain b)
          (:init (at a) (edge a b) (edge b c) (target c))
          (:tasks (walk a)))
        """
        result = solve(BOUNDS_DOMAIN, problem, max_depth=10, max_actions=1)
        assert result.status == "inconclusive"
        assert {u.category for u in result.uncertain} == {"action_budget"}


# --------------------------------------------------------------------------- #
# Reference validation
# --------------------------------------------------------------------------- #


class TestReferenceValidation:
    def test_unknown_operator_in_root_raises(self) -> None:
        with pytest.raises(PlanningError, match="unknown operator"):
            solve(
                "(:domain d (:operator (!a) (:pre) (:del) (:add)))",
                "(:problem p (:domain d) (:init) (:tasks (!zzz)))",
            )

    def test_unknown_compound_task_raises(self) -> None:
        with pytest.raises(PlanningError, match="no method"):
            solve(
                "(:domain d (:operator (!a) (:pre) (:del) (:add)))",
                "(:problem p (:domain d) (:init) (:tasks (ghost)))",
            )

    def test_wrong_arity_raises(self) -> None:
        with pytest.raises(PlanningError, match="expects"):
            solve(
                "(:domain d (:operator (!a ?x) (:pre) (:del) (:add (q ?x))))",
                "(:problem p (:domain d) (:init) (:tasks (!a 1 2)))",
            )

    def test_domain_name_mismatch_raises(self) -> None:
        with pytest.raises(PlanningError, match="references domain"):
            Planner().solve(
                parse_domain("(:domain one (:operator (!a) (:pre) (:del) (:add)))"),
                parse_problem("(:problem p (:domain two) (:init) (:tasks (!a)))"),
                "req",
            )


# --------------------------------------------------------------------------- #
# Empty (no-op) methods
# --------------------------------------------------------------------------- #

EMPTY_DOMAIN = """
(:domain empty
  (:operator (!a) (:pre) (:del) (:add (pa)))
  (:operator (!b) (:pre (pa)) (:del (pa)) (:add (pb)))
  ;; no-op reduction: an already-satisfied task disappears
  (:method m-skip (maybe ?x) (:pre (skipped ?x)) (:tasks))
  (:method m-do   (maybe ?x) (:pre (not (skipped ?x))) (:tasks (!a)))
  (:method m-seq (run ?x) (:pre) (:tasks (maybe ?x) (!b))))
"""


class TestEmptyMethod:
    def test_noop_method_disappears_and_releases_successor(self) -> None:
        # maybe is skipped (empty), so !b follows directly; but !b needs (pa),
        # which the skipped branch never establishes -> deadlock. This proves
        # the empty task vanished (it cannot itself run), not that it was
        # treated as an executable primitive.
        result = solve(
            EMPTY_DOMAIN,
            "(:problem p (:domain empty) (:init (skipped g)) (:tasks (run g)))",
        )
        assert result.status == "failure"
        assert result.failures[0].category == "deadlock"

    def test_nonempty_branch_satisfies_successor(self) -> None:
        result = solve(
            EMPTY_DOMAIN,
            "(:problem p (:domain empty) (:init) (:tasks (run g)))",
        )
        assert result.status == "success"
        assert [a.operator for a in result.plan] == ["!a", "!b"]


class TestEmptyRootNetwork:
    def test_empty_root_tasks_is_trivial_success(self) -> None:
        domain = "(:domain d (:operator (!a) (:pre) (:del) (:add (x))))"
        result = solve(
            domain, "(:problem p (:domain d) (:init) (:tasks))"
        )
        assert result.status == "success"
        assert result.plan == ()
        # The expansion tree is just the empty network root.
        assert [r.kind for r in result.expansion] == ["network"]


# --------------------------------------------------------------------------- #
# Expansion tree shape
# --------------------------------------------------------------------------- #


class TestExpansionTree:
    def test_tree_records_abstract_to_primitive_chain(self) -> None:
        result = solve(CHAIN_DOMAIN, """
        (:problem p (:domain chain)
          (:init (avail a) (next a b) (avail b) (last b))
          (:tasks (proc a)))
        """, max_depth=5)
        by_kind: dict[str, list[str]] = {}
        for rec in result.expansion:
            by_kind.setdefault(rec.kind, []).append(rec.expanded_by or "")
        assert "network" in by_kind
        compounds = [r for r in result.expansion if r.kind == "compound"]
        primitives = [r for r in result.expansion if r.kind == "primitive"]
        assert {r.expanded_by for r in compounds} == {"m-step", "m-last"}
        assert len(primitives) == 2
        # Parent linkage reaches the network root.
        ids = {r.node_id for r in result.expansion}
        assert "root" in ids
        for rec in compounds + primitives:
            assert rec.parent_id in ids
