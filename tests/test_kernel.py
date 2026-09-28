"""Unit tests for the reasoning kernel: concrete results, not just callability."""
from __future__ import annotations

import pytest

from app.kernel import (
    CLASS_UNSATISFIABLE,
    ONTOLOGY_INCONSISTENT,
    reason,
)
from app.language import parse_ontology

from .conftest import load_fixture


def _reason(name: str):
    return reason(parse_ontology(load_fixture(name)), engine_version="test")


def _unsat_names(result) -> set[str]:
    return set(result.unsatisfiable_nodes)


# ---------------------------------------------------------------------------
# Fixture: multi-level inheritance
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_multi_inheritance_penguin_chain_is_satisfiable():
    r = _reason("multi_inheritance")
    assert "Penguin" not in _unsat_names(r)
    assert "EmperorPenguin" not in _unsat_names(r)
    assert r.is_subclass("EmperorPenguin", "Penguin")
    assert r.is_subclass("EmperorPenguin", "Swimmer")
    assert r.is_subclass("EmperorPenguin", "Bird")


@pytest.mark.unit
def test_multi_inheritance_dodo_unsatisfiable_but_ontology_consistent():
    r = _reason("multi_inheritance")
    assert "Dodo" in _unsat_names(r)
    assert CLASS_UNSATISFIABLE in r.failure_categories
    # No instance of Dodo -> ontology itself is still consistent
    assert r.consistent is True
    assert ONTOLOGY_INCONSISTENT not in r.failure_categories


@pytest.mark.unit
def test_dodo_conflict_path_names_both_disjoint_sides():
    r = _reason("multi_inheritance")
    report = next(u for u in r.unsatisfiable if u.cls == "Dodo")
    clash = report.conflict.disjoint_classes
    assert set(clash) == {"FlightedBird", "FlightlessBird"}
    assert report.conflict.disjoint_source == "fixture:flighted-flightless"
    # chains are non-empty and every hop carries an axiom source
    assert report.conflict.left_chain and report.conflict.right_chain
    assert all(step.source for step in report.conflict.left_chain)


# ---------------------------------------------------------------------------
# Fixture: equivalence ring
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_equivalence_ring_merges_all_three_and_keeps_sources():
    r = _reason("equivalence_ring")
    assert len(r.equivalences) == 1
    eq = r.equivalences[0]
    assert set(eq.members) == {"Admin", "SuperUser", "RootUser"}
    # both declaring axioms survive the merge
    assert set(eq.declaration_sources) == {
        "fixture:eq-admin-superuser",
        "fixture:eq-superuser-root",
    }
    # merge chain records two hops with their origins
    assert {m.source for m in eq.merge_chain} == {
        "fixture:eq-admin-superuser",
        "fixture:eq-superuser-root",
    }


@pytest.mark.unit
def test_equivalence_ring_is_unsatisfiable_consistent():
    r = _reason("equivalence_ring")
    ring = set(r.equivalences[0].members)
    assert ring <= _unsat_names(r)
    assert r.consistent is True
    # the healthy Guest instance still infers its superclass
    visitor = next(i for i in r.instances if i.instance == "visitor-42")
    assert visitor.status == "satisfiable"
    assert set(visitor.entailed_types) >= {"Guest", "User"}


# ---------------------------------------------------------------------------
# Fixture: intersection + disjoint conflict with instance
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_intersection_ta_unsatisfiable_and_inconsistent():
    r = _reason("intersection_disjoint_conflict")
    assert "TA" in _unsat_names(r)
    assert r.consistent is False
    assert set(r.failure_categories) == {
        CLASS_UNSATISFIABLE, ONTOLOGY_INCONSISTENT,
    }


@pytest.mark.unit
def test_intersection_conflict_path_traverses_operands():
    r = _reason("intersection_disjoint_conflict")
    alice = next(i for i in r.instances if i.instance == "alice-7")
    assert alice.status == "in_conflict"
    clash = alice.conflict
    assert clash.category == ONTOLOGY_INCONSISTENT
    assert set(clash.disjoint_classes) == {"Employee", "Student"}
    assert clash.disjoint_source == "fixture:employee-student-disjoint"
    # each side derives via the intersection node
    left_targets = {s.to for s in clash.left_chain}
    right_targets = {s.to for s in clash.right_chain}
    assert "Employee" in left_targets | right_targets
    assert "Student" in left_targets | right_targets


@pytest.mark.unit
def test_unrelated_instance_remains_consistent():
    r = _reason("intersection_disjoint_conflict")
    bob = next(i for i in r.instances if i.instance == "bob-9")
    assert bob.status == "satisfiable"
    assert "Person" in bob.entailed_types


# ---------------------------------------------------------------------------
# Fixture: two assertions on one instance clash without any unsat class
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_instance_only_clash_no_unsatisfiable_class():
    r = _reason("two_assertions_conflict")
    assert r.unsatisfiable == ()
    assert CLASS_UNSATISFIABLE not in r.failure_categories
    assert r.consistent is False
    assert ONTOLOGY_INCONSISTENT in r.failure_categories
    carol = r.instances[0]
    assert set(carol.conflict.disjoint_classes) == {"Cat", "Dog"}


# ---------------------------------------------------------------------------
# Equivalence collapsing two disjoint operands
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_disjoint_operands_merged_by_equivalence():
    payload = {
        "axioms": [
            {"equivalent": ["A", "B"], "source": "eq"},
            {"disjoint": ["A", "B"], "source": "dj"},
        ]
    }
    r = reason(parse_ontology(payload))
    assert {"A", "B"} <= _unsat_names(r)
    assert r.consistent is True  # no instance, no witnessed contradiction


# ---------------------------------------------------------------------------
# Intersection introduction is needed (disjoint operand is an intersection)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_intersection_introduction_rule_fires():
    payload = {"axioms": [
        {"sub": "C", "super": "A"},
        {"sub": "C", "super": "B"},
        {"disjoint": [{"intersection": ["A", "B"]}, "D"]},
        {"sub": "C", "super": "D"},
    ]}
    r = reason(parse_ontology(payload))
    assert "C" in _unsat_names(r)


@pytest.mark.unit
def test_unsatisfiable_subclass_entails_everything_vacuously():
    payload = {"axioms": [
        {"sub": "U", "super": "A"},
        {"sub": "U", "super": "B"},
        {"disjoint": ["A", "B"]},
        # X exists in the signature but has no connection to U
        {"instance": "x1", "class": "X"},
    ]}
    r = reason(parse_ontology(payload))
    assert r.is_subclass("U", "X") is True
    assert r.is_subclass("A", "B") is False
