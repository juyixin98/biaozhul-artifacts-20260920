"""Kernel tests with concrete expected answers and an independent oracle.

The expected action sequences here are hand-derived literals; feasibility of a
returned order is additionally re-checked by ``tests.oracle`` which does not use
the planner under test.
"""
from __future__ import annotations

import itertools

import pytest

pytestmark = pytest.mark.kernel

from htn_planner.models import (
    Condition,
    Effect,
    Method,
    Primitive,
    Problem,
    Subtask,
    Domain,
)
from htn_planner.planner import Planner

from .fixture_loader import domain_path, load_raw_problem, plan_fixture
from .oracle import IndependentOracle, OracleFailure


def _oracle_for(problem_file: str) -> tuple[IndependentOracle, dict]:
    raw = load_raw_problem(problem_file)
    oracle = IndependentOracle(
        domain_path(raw["domain"]),
        raw.get("initial_facts", []),
        raw.get("initial_resources"),
    )
    return oracle, raw


def _grounded(result) -> list[tuple[str, tuple[str, ...]]]:
    return [
        (result.nodes[n].primitive, tuple(result.nodes[n].args))
        for n in result.execution_order
    ]


# ---------------------------------------------------------------------------
# Feasible cases: exact results, independently re-simulated
# ---------------------------------------------------------------------------
def test_direct_shipment_has_exact_action_sequence() -> None:
    result, _, _ = plan_fixture("logistics_direct.yaml")
    assert result.feasible is True
    names = [a[0] for a in _grounded(result)]
    assert names == [
        "acquire_dock", "embark", "release_dock", "move",
        "acquire_dock", "disembark", "release_dock",
    ]
    grounded = _grounded(result)
    assert ("move", ("truck", "depot", "city")) in grounded
    # Exactly one haul leg: the direct method, no hub hop.
    assert grounded.count(("move", ("truck", "depot", "hub"))) == 0

    oracle, _ = _oracle_for("logistics_direct.yaml")
    oracle.replay(result.model_dump()["nodes"], result.execution_order)  # raises if infeasible
    # Cargo ends at the city under the oracle's own bookkeeping.
    assert ("at", "box", "city") in oracle.facts
    assert oracle.max_held["dock"] <= 1


def test_via_hub_recurses_with_concrete_two_leg_route() -> None:
    result, _, _ = plan_fixture("logistics_via_hub.yaml")
    assert result.feasible is True
    grounded = _grounded(result)
    moves = [a for a in grounded if a[0] == "move"]
    assert moves == [
        ("move", ("truck", "depot", "hub")),
        ("move", ("truck", "hub", "city")),
    ]
    used = {n.method for n in result.nodes.values() if n.method}
    assert "m-haul-via-hub" in used and "m-haul-direct" in used
    assert result.depth_used == 3 and result.expansions_used == 3

    oracle, _ = _oracle_for("logistics_via_hub.yaml")
    oracle.replay(result.model_dump()["nodes"], result.execution_order)
    assert ("at", "box", "city") in oracle.facts
    # Rejected direct alternative is retained as an abandoned branch, not lost.
    assert any(
        f.task == "haul" and f.method == "m-haul-direct"
        for f in result.abandoned_branches
    )


def test_methods_are_mutually_exclusive_by_state() -> None:
    direct, _, _ = plan_fixture("logistics_direct.yaml")
    via, _, _ = plan_fixture("logistics_via_hub.yaml")
    haul_direct = next(
        n for n in direct.nodes.values() if n.task == "haul"
    )
    haul_via = next(
        n for n in direct.nodes.values() if False
    ) if False else next(n for n in via.nodes.values() if n.task == "haul")
    assert haul_direct.method == "m-haul-direct"
    assert haul_via.method == "m-haul-via-hub"


def test_partial_order_two_shipments_feasible_and_serializes_dock() -> None:
    result, _, _ = plan_fixture("logistics_partial_ok.yaml")
    assert result.feasible is True
    assert len(result.execution_order) == 14
    oracle, _ = _oracle_for("logistics_partial_ok.yaml")
    oracle.replay(result.model_dump()["nodes"], result.execution_order)
    # Capacity-1 dock is never over-subscribed across interleaved siblings.
    assert oracle.max_held["dock"] == 1

    names = [f"{a[0]}{a[1]}" for a in _grounded(result)]
    def index_of(action: str) -> int:
        return next(i for i, n in enumerate(names) if n == action)

    # Each shipment's internal sequential chain is preserved in execution.
    chain1 = ["acquire_dock('truck1',)", "embark('box1', 'truck1', 'depot')",
              "release_dock('truck1',)"]
    pos1 = [index_of(a) for a in chain1]
    assert pos1 == sorted(pos1)
    chain2 = ["acquire_dock('truck2',)", "embark('box2', 'truck2', 'depot')",
              "release_dock('truck2',)"]
    pos2 = [index_of(a) for a in chain2]
    assert pos2 == sorted(pos2)


def test_deep_assembly_expands_abstract_tree_to_five_leaves() -> None:
    result, _, _ = plan_fixture("assembly_deep.yaml")
    assert result.feasible is True
    assert _grounded(result) == [
        ("take_part", ("leaf",)),
        ("join", ("leaf", "a3")),
        ("join", ("a3", "a2")),
        ("join", ("a2", "a1")),
        ("join", ("a1", "root")),
    ]
    compounds = [n for n in result.nodes.values() if n.kind == "compound"]
    leaves = [n for n in result.nodes.values() if n.kind == "primitive"]
    assert len(compounds) == 5 and len(leaves) == 5
    # The retained tree links the root abstract task down through a1..a3.
    root = result.nodes[result.roots[0]]
    assert root.task == "assemble" and root.method == "m-assemble-composite"
    chain_methods = [
        result.nodes[c].method for c in _walk_children(result, root.node_id)
        if result.nodes[c].kind == "compound"
    ]
    assert chain_methods.count("m-assemble-composite") == 4

    oracle, _ = _oracle_for("assembly_deep.yaml")
    oracle.replay(result.model_dump()["nodes"], result.execution_order)
    assert ("ready", "root") in oracle.facts


def _walk_children(result, node_id):
    out = []
    stack = [node_id]
    while stack:
        cur = stack.pop()
        out.append(cur)
        stack.extend(result.nodes[cur].children)
    return out


def test_retained_tree_contains_only_reachable_nodes() -> None:
    result, _, _ = plan_fixture("logistics_via_hub.yaml")
    reachable = set(_walk_children(result, result.roots[0]))
    assert set(result.nodes) == reachable
    assert all(n.status != "failed" for n in result.nodes.values())


# ---------------------------------------------------------------------------
# Partial order: a topological order is NOT executability
# ---------------------------------------------------------------------------
def test_topological_orders_exist_but_no_order_executes_for_token_conflict() -> None:
    result, _, _ = plan_fixture("logistics_partial_conflict.yaml")
    assert result.feasible is False
    assert result.execution_order == []
    kinds = [f.kind.value for f in result.failures]
    assert "partial_order_infeasible" in kinds
    # Both ready placements failed on a concrete state precondition.
    assert kinds.count("precondition_not_stat") == 2

    # Independent demonstration: the sibling DAG (two nodes, no edge) has two
    # topological linearizations, and the oracle proves BOTH fail to execute.
    raw = load_raw_problem("logistics_partial_conflict.yaml")
    perms = list(itertools.permutations(["a", "b"]))
    assert len(perms) == 2  # structurally, both orders are valid topo orders
    for order in perms:
        oracle = IndependentOracle(domain_path(raw["domain"]),
                                   raw["initial_facts"], None)
        # Build a synthetic node map mirroring the two consume_token leaves.
        who = {"a": "alice", "b": "bob"}
        nodes = {
            sid: {
                "kind": "primitive", "primitive": "consume_token",
                "args": [who[sid]], "children": [],
            }
            for sid in order
        }
        with pytest.raises(OracleFailure, match="token_ready"):
            oracle.replay(nodes, list(order))


def test_shared_resource_conflict_is_partial_order_infeasible() -> None:
    result, _, _ = plan_fixture("logistics_resource_conflict.yaml")
    assert result.feasible is False
    kinds = [f.kind.value for f in result.failures]
    assert "partial_order_infeasible" in kinds
    assert "resource_unavailable" in kinds
    detail = next(f.detail for f in result.failures
                  if f.kind.value == "resource_unavailable")
    assert "capacity=1 held=1" in detail


# ---------------------------------------------------------------------------
# Failure categories are specific
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "fixture,terminal",
    [
        ("logistics_no_viable.yaml", "no_viable_method"),
        ("logistics_unresolvable.yaml", "unresolvable_task"),
        ("logistics_precondition_fail.yaml", "precondition_not_stat"),
        ("logistics_cycle.yaml", "method_cycle"),
        ("assembly_depth_capped.yaml", "depth_exceeded"),
        ("assembly_budget_tight.yaml", "expansion_budget_exhausted"),
    ],
)
def test_infeasible_fixtures_have_expected_terminal_category(
    fixture: str, terminal: str
) -> None:
    result, _, _ = plan_fixture(fixture)
    assert result.feasible is False
    assert result.failures[-1].kind.value == terminal


def test_no_viable_method_lists_both_alternatives_and_guard_reasons() -> None:
    result, _, _ = plan_fixture("logistics_no_viable.yaml")
    summary = result.failures[-1]
    assert summary.tried_methods == ["m-haul-direct", "m-haul-via-hub"]
    rejected = {r["method"] for r in summary.rejected_guards}
    assert rejected == {"m-haul-direct", "m-haul-via-hub"}


def test_cycle_evidence_carries_node_path_and_is_not_stack_overflow() -> None:
    result, _, _ = plan_fixture("logistics_cycle.yaml")
    cycle = next(f for f in result.failures if f.kind.value == "method_cycle")
    assert cycle.node_path[0].startswith("loop_forever")
    assert any("m-loop-forever" in step for step in cycle.node_path)
    assert result.depth_used <= 2  # bounded immediately, not deep recursion


def test_depth_and_budget_are_reported_as_bounds() -> None:
    depth, _, _ = plan_fixture("assembly_depth_capped.yaml")
    budget, _, _ = plan_fixture("assembly_budget_tight.yaml")
    assert depth.failures[-1].detail.count("depth") >= 1
    assert budget.expansions_used == 1


# ---------------------------------------------------------------------------
# Backtracking: guard passes, body fails, a later method still wins
# ---------------------------------------------------------------------------
def _backtrack_domain() -> Domain:
    fail_prim = Primitive(
        name="needs_x",
        precondition=[Condition(kind="fact", name="x")],
    )
    win_prim = Primitive(
        name="finish",
        effect=Effect(add=[["done"]]),
    )
    return Domain(
        name="backtrack",
        primitives={"needs_x": fail_prim, "finish": win_prim},
        methods=[
            Method(
                name="m-first-loses", task="goal",
                subtasks=[Subtask(id="s", task="needs_x")],
            ),
            Method(
                name="m-second-wins", task="goal",
                subtasks=[Subtask(id="s", task="finish")],
            ),
        ],
    )


def test_planner_backtracks_from_failed_branch_to_viable_method() -> None:
    problem = Problem(name="bt", domain="backtrack", goal_task="goal")
    result = Planner(_backtrack_domain()).plan(problem)
    assert result.feasible is True
    root = result.nodes[result.roots[0]]
    assert root.method == "m-second-wins"
    # The losing branch's concrete precondition failure is retained; the leaf
    # evidence names the task, and its node_path records the losing method.
    losing = [
        f for f in result.abandoned_branches
        if f.kind.value == "precondition_not_stat"
        and any("m-first-loses" in step for step in f.node_path)
    ]
    assert losing and losing[0].task == "needs_x"
    assert _grounded(result) == [("finish", ())]


# ---------------------------------------------------------------------------
# Partial-order edge constraints are enforced through the hierarchy
# ---------------------------------------------------------------------------
def test_partial_order_edge_forces_producer_before_consumer() -> None:
    produce = Primitive(
        name="produce", effect=Effect(add=[["ready"]])
    )
    consume = Primitive(
        name="consume",
        precondition=[Condition(kind="fact", name="ready")],
        effect=Effect(remove=[["ready"]]),
    )
    domain = Domain(
        name="po",
        primitives={"produce": produce, "consume": consume},
        methods=[
            Method(
                name="m-ordered", task="goal", order="partial",
                subtasks=[
                    Subtask(id="p", task="produce"),
                    Subtask(id="c", task="consume", after=["p"]),
                ],
            )
        ],
    )
    result = Planner(domain).plan(
        Problem(name="po", domain="po", goal_task="goal")
    )
    assert result.feasible is True
    assert _grounded(result) == [("produce", ()), ("consume", ())]
    consumer = next(n for n in result.nodes.values() if n.primitive == "consume")
    producer = next(n for n in result.nodes.values() if n.primitive == "produce")
    assert producer.node_id in consumer.after
