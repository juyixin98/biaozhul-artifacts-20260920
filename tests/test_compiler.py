"""Tests for the rule compiler: flattening, rule shapes, provenance anchors."""

from __future__ import annotations

from min_iowl.lang.compiler import BOTTOM, compile_ontology
from min_iowl.lang.parser import parse_axiom

from .conftest import functional_items


def test_subclass_intersection_to_conjunctive_rules() -> None:
    items = functional_items("SubClassOf(ObjectIntersectionOf(A B) C)")
    program = compile_ontology(items)
    assert len(program.rules) == 1
    rule = program.rules[0]
    assert rule.body == ("A", "B")
    assert rule.heads == ("C",)
    assert rule.kind == "sub"
    assert rule.source_axiom == "a001"


def test_superclass_intersection_expands_one_rule_per_conjunct() -> None:
    items = functional_items("SubClassOf(A ObjectIntersectionOf(B C))")
    program = compile_ontology(items)
    bodies_heads = sorted((r.body, r.heads) for r in program.rules)
    assert bodies_heads == [(("A",), ("B",)), (("A",), ("C",))]


def test_equivalence_emits_both_directions_with_same_source() -> None:
    items = functional_items("EquivalentClasses(A B)")
    program = compile_ontology(items)
    dirs = {(r.body, r.heads[0]) for r in program.rules if r.kind == "equiv"}
    assert dirs == {(("A",), "B"), (("B",), "A")}
    # both directions keep the SAME declared source axiom
    assert {r.source_axiom for r in program.rules} == {"a001"}


def test_three_class_equivalence_ring_is_pairwise_closure() -> None:
    items = functional_items("EquivalentClasses(A B C)")
    program = compile_ontology(items)
    assert len(program.rules) == 6  # 3*2 directed edges
    pairs = {(r.body[0], r.heads[0]) for r in program.rules}
    assert pairs == {("A", "B"), ("B", "A"), ("A", "C"), ("C", "A"),
                     ("B", "C"), ("C", "B")}


def test_disjoint_compiles_to_bottom_rule_with_pair() -> None:
    items = functional_items("DisjointClasses(A B)")
    program = compile_ontology(items)
    assert len(program.rules) == 1
    rule = program.rules[0]
    assert rule.is_bottom
    assert rule.heads == ()
    assert rule.body == ("A", "B")
    assert rule.disjoint_pair == ("A", "B")


def test_disjoint_three_classes_emits_all_pairs() -> None:
    items = functional_items("DisjointClasses(A B C)")
    program = compile_ontology(items)
    assert len(program.rules) == 3
    pairs = {tuple(sorted(r.body)) for r in program.rules}
    assert pairs == {("A", "B"), ("A", "C"), ("B", "C")}


def test_class_assertion_becomes_base_facts_preserving_source() -> None:
    items = functional_items("ClassAssertion(ObjectIntersectionOf(A B) x)")
    program = compile_ontology(items)
    preds = sorted(f.predicate for f in program.facts)
    assert preds == ["A", "B"]
    assert all(f.individual == "x" and f.source_axiom == "a001" for f in program.facts)
    assert program.individuals == frozenset({"x"})


def test_class_sources_anchor_every_declared_axiom() -> None:
    items = functional_items(
        "SubClassOf(A B)\nEquivalentClasses(B C)\nDisjointClasses(C D)"
    )
    program = compile_ontology(items)
    assert program.class_sources["B"] == ("a001", "a002")
    assert program.class_sources["D"] == ("a003",)


def test_tautological_self_rule_is_dropped() -> None:
    items = functional_items("SubClassOf(A A)")
    assert compile_ontology(items).rules == ()


def test_no_bottom_predicate_leaks_into_declared_classes() -> None:
    items = functional_items("DisjointClasses(A B)")
    program = compile_ontology(items)
    assert BOTTOM not in program.declared_classes
