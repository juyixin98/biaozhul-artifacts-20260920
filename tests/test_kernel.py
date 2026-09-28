"""Kernel tests: saturation, state distinctions, conflict paths, equivalence."""

from __future__ import annotations

from min_iowl.kernel import engine
from min_iowl.kernel.engine import BaseNode, DerivedNode
from min_iowl.kernel.equivalence import build_equivalence_groups
from min_iowl.lang.compiler import compile_ontology

from .conftest import fixture_items, functional_items


def _run(items):
    program = compile_ontology(items)
    report = engine.saturate(program)
    groups = build_equivalence_groups(program, report)
    return program, report, groups


# ------------------------------------------------------- multi-level chain --
def test_multi_level_inheritance_is_transitive() -> None:
    items = functional_items(
        "SubClassOf(Dog Mammal)\n"
        "SubClassOf(Mammal Animal)\n"
        "SubClassOf(Animal Organism)\n"
        "ClassAssertion(Dog rex)"
    )
    _, report, _ = _run(items)
    rex = report.individual("rex")
    assert rex is not None
    assert rex.types == frozenset({"Dog", "Mammal", "Animal", "Organism"})
    assert rex.conflict is None
    # TBox view agrees
    assert report.tbox_supers["Dog"] == frozenset({"Mammal", "Animal", "Organism"})


def test_equivalence_ring_propagates_in_all_directions() -> None:
    items = functional_items(
        "EquivalentClasses(HomoSapiens Human Person)\n"
        "SubClassOf(Human Mammal)\n"
        "ClassAssertion(Person alice)"
    )
    _, report, groups = _run(items)
    alice = report.individual("alice")
    assert alice.types == frozenset(
        {"Person", "Human", "HomoSapiens", "Mammal"}
    )
    rings = [g for g in groups if len(g.members) > 1]
    assert len(rings) == 1
    assert set(rings[0].members) == {"HomoSapiens", "Human", "Person"}


def test_equivalence_group_preserves_declared_source() -> None:
    items = functional_items("EquivalentClasses(A B)\nEquivalentClasses(B C)")
    _, report, groups = _run(items)
    nontrivial = [g for g in groups if len(g.members) > 1]
    assert len(nontrivial) == 1
    group = nontrivial[0]
    assert set(group.members) == {"A", "B", "C"}
    # the merge must retain BOTH original declarations, not collapse them
    assert set(group.direct_equivalence_axioms) == {"a001", "a002"}


def test_indirect_equivalence_through_subclass_cycle_is_detected() -> None:
    # A <= B and B <= A declared as plain subclass axioms -> equivalent anyway
    items = functional_items("SubClassOf(A B)\nSubClassOf(B A)")
    _, report, groups = _run(items)
    assert report.tbox_supers["A"] == frozenset({"B"})
    assert report.tbox_supers["B"] == frozenset({"A"})
    merged = {g.members for g in groups if len(g.members) > 1}
    assert merged == {("A", "B")}


# ---------------------------- class-unsat vs ontology-inconsistent distinction
def test_unsatisfiable_class_does_not_make_ontology_inconsistent() -> None:
    items = fixture_items("fixture_intersection_disjoint.json")
    _, report, _ = _run(items)
    # Hermaphrodite <= Male and <= Female, Male disjoint Female
    assert [u.cls for u in report.unsatisfiable] == ["Hermaphrodite"]
    assert report.inconsistent is False  # no instance -> ontology has a model


def test_unsatisfiable_class_conflict_path_names_both_sources() -> None:
    items = fixture_items("fixture_intersection_disjoint.json")
    _, report, _ = _run(items)
    unsat = report.unsatisfiable[0]
    sources = set(unsat.conflict.sources)
    # the disjoint axiom and both subclass restrictions must all be evidenced
    assert "a007" in sources and "a008" in sources  # Hermaphrodite <= Male/Female
    assert unsat.conflict.disjoint_pair == ("Male", "Female")
    # hypothesis root marks this as class-level, not a real individual
    roots = unsat.conflict.literal_proofs
    assert any(
        isinstance(n, DerivedNode)
        and any(isinstance(c, BaseNode) and c.kind == "hypothesis" for c in n.children)
        for n in roots
    )


def test_mutex_instance_makes_ontology_inconsistent_with_path() -> None:
    items = fixture_items("fixture_mutex_instance.json")
    _, report, _ = _run(items)
    mallory = report.individual("mallory")
    assert mallory.conflict is not None
    assert report.inconsistent is True
    # no *class* needs to be unsatisfiable: Father and Mother each have models
    assert report.unsatisfiable == ()
    path = mallory.conflict
    assert path.disjoint_pair == ("Male", "Female")
    # the two assertions forcing the conflict are both on the path
    sources = set(path.sources)
    assert "a006" in sources and "a007" in sources  # the two ClassAssertions
    # and an uninvolved individual stays conflict-free
    assert report.individual("nina").conflict is None


def test_conflict_path_has_proof_for_every_disjoint_operand() -> None:
    items = fixture_items("fixture_mutex_instance.json")
    _, report, _ = _run(items)
    path = report.individual("mallory").conflict
    predicates = {node.predicate for node in path.literal_proofs}
    assert predicates == {"Male", "Female"}


def test_proof_tree_leaves_are_asserted_axiom_sources() -> None:
    items = fixture_items("fixture_mutex_instance.json")
    _, report, _ = _run(items)
    male_proof = report.individual("mallory").proofs["Male"]

    def leaves(node):
        if isinstance(node, BaseNode):
            return [node]
        out = []
        for c in node.children:
            out.extend(leaves(c))
        return out

    leaf_sources = {n.source_axiom for n in leaves(male_proof)}
    assert leaf_sources == {"a007"}  # mallory : Father assertion


# ------------------------------------------------------------- intersection --
def test_intersection_assertion_derives_named_class() -> None:
    items = functional_items(
        "SubClassOf(ObjectIntersectionOf(Parent Male) Father)\n"
        "ClassAssertion(ObjectIntersectionOf(Parent Male) bob)"
    )
    _, report, _ = _run(items)
    assert "Father" in report.individual("bob").types


def test_intersection_requires_all_conjuncts_to_fire() -> None:
    items = functional_items(
        "SubClassOf(ObjectIntersectionOf(Parent Male) Father)\n"
        "ClassAssertion(Male bob)"  # Parent missing -> rule must not fire
    )
    _, report, _ = _run(items)
    assert "Father" not in report.individual("bob").types


def test_equivalence_with_intersection_is_bidirectional() -> None:
    items = functional_items(
        "EquivalentClasses(Father ObjectIntersectionOf(Male Parent))\n"
        "ClassAssertion(Father bob)"
    )
    _, report, _ = _run(items)
    assert report.individual("bob").types == frozenset({"Father", "Male", "Parent"})
