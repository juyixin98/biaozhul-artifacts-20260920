"""Proof-tree edge cases and relational matching guards."""

import pytest

pytestmark = pytest.mark.unit

from conftest import load_fixture
from datalog_service.engine.derivations import build_proof_tree
from datalog_service.engine.relational import match_terms
from datalog_service.language.ast import Constant, Variable
from datalog_service.language.parser import parse_program
from datalog_service.language.compiler import compile_program
from datalog_service.engine.fixpoint import evaluate


def _ancestor_materialization():
    program = parse_program(load_fixture("ancestor.dl"))
    compiled = compile_program(program)
    return compiled, evaluate(compiled, program.facts)


def test_unknown_tuple_is_marked_unexplained_not_fabricated():
    compiled, mat = _ancestor_materialization()
    tree = build_proof_tree(
        "ancestor", ("ghost", "ghost"), mat.derivations,
        {cr.rule_id: cr.text for cr in compiled.rules},
    )
    assert tree.kind == "unexplained"


def test_deep_proof_is_truncated_and_flagged():
    compiled, mat = _ancestor_materialization()
    tree = build_proof_tree(
        "ancestor", ("alice", "erin"), mat.derivations,
        {cr.rule_id: cr.text for cr in compiled.rules},
        max_depth=1,
    )
    # The root derives via r2; after one step the bound trips.
    kinds = _all_kinds(tree)
    assert "depth_truncated" in kinds
    truncated = _first(tree, "depth_truncated")
    assert truncated.rule_id is not None


def test_truncated_proof_serializes_rule_text():
    compiled, mat = _ancestor_materialization()
    tree = build_proof_tree(
        "ancestor", ("alice", "erin"), mat.derivations,
        {cr.rule_id: cr.text for cr in compiled.rules},
        max_depth=1,
    )
    as_dict = tree.to_dict()
    truncated = [
        n for n in _flatten_dicts(as_dict) if n["kind"] == "depth_truncated"
    ]
    assert truncated and truncated[0]["rule_text"]


def test_match_terms_rejects_wrong_length():
    terms = (Variable("X"), Constant("a"))
    assert match_terms(terms, (1,)) is None
    assert match_terms(terms, (1, "a", 2)) is None


def test_match_terms_unifies_repeated_variable():
    terms = (Variable("X"), Variable("X"))
    assert match_terms(terms, (1, 2)) is None
    assert match_terms(terms, (1, 1)) == {"X": 1}


def test_match_terms_rejects_constant_clash():
    terms = (Variable("X"), Constant("a"))
    assert match_terms(terms, (1, "b")) is None
    assert match_terms(terms, (1, "a")) == {"X": 1}


def _all_kinds(node):
    kinds = {node.kind}
    for child in node.children:
        kinds |= _all_kinds(child)
    return kinds


def _first(node, kind):
    if node.kind == kind:
        return node
    for child in node.children:
        found = _first(child, kind)
        if found is not None:
            return found
    return None


def _flatten_dicts(node):
    nodes = [node]
    for child in node.get("children", []):
        nodes.extend(_flatten_dicts(child))
    return nodes



def test_deep_chain_proof_is_iteration_safe():
    # A 3,000-deep synthetic derivation chain must build, serialize and walk
    # without RecursionError (all tree traversals are iterative).
    from datalog_service.engine.derivations import (
        BodyBinding, Derivation, build_proof_tree, leaves,
    )
    n = 3000
    derivs = {
        ("r", (f"n{n}",)): Derivation(
            Derivation.FACT_RULE, ("r", (f"n{n}",)), (), 0, 0
        )
    }
    for i in range(n - 1, -1, -1):
        row = (f"n{i}",)
        bindings = (
            BodyBinding("p", (f"n{i}", f"n{i+1}"), False),
            BodyBinding("r", (f"n{i+1}",), False),
        )
        derivs[("r", row)] = Derivation(
            "r1", ("r", row), bindings, n - i, 0
        )
    tree = build_proof_tree(
        "r", ("n0",), derivs, {"r1": "r(X) :- p(X, Y), r(Y)."},
        max_depth=n + 10,
    )
    assert tree.kind == "derived"
    assert len(leaves(tree)) == n + 1
    serialized = tree.to_dict()  # must not raise RecursionError
    assert serialized["goal"] == "r(n0)"
