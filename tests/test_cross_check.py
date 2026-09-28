"""Cross-check: the forward-chaining kernel vs the independent enumerator.

Every reference answer below comes from the independent enumerator (which only
uses the shared syntactic AST); these tests assert the kernel matches it on
multi-level inheritance, equivalence rings, intersections, and both kinds of
disjointness failure.
"""

from __future__ import annotations

import pytest

from min_iowl.kernel import engine
from min_iowl.lang.compiler import compile_ontology
from min_iowl.oracle.enumerator import enumerate_ontology, normalize
from min_iowl.service.reasoning import ReasoningService

from .conftest import fixture_items, functional_items


def _kernel_and_oracle(items):
    nodes = [a for _, a in items]
    program = compile_ontology(items)
    report = engine.saturate(program)
    verdict = enumerate_ontology(normalize(nodes))
    return program, report, verdict


def test_satisfiable_classes_agree() -> None:
    for name in [
        "fixture_hierarchy.json",
        "fixture_intersection_disjoint.json",
        "fixture_mutex_instance.json",
    ]:
        items = fixture_items(name)
        _, report, verdict = _kernel_and_oracle(items)
        kernel_unsat = {u.cls for u in report.unsatisfiable}
        oracle_unsat = {c.cls for c in verdict.classes if not c.satisfiable}
        assert kernel_unsat == oracle_unsat, name


def test_ontology_consistency_agrees() -> None:
    expected = {
        "fixture_hierarchy.json": True,
        "fixture_intersection_disjoint.json": True,   # unsat class, no instance
        "fixture_mutex_instance.json": False,         # real instance conflict
    }
    for name, consistent in expected.items():
        items = fixture_items(name)
        _, report, verdict = _kernel_and_oracle(items)
        assert report.inconsistent is (not consistent)
        assert verdict.ontology_consistent is consistent


def test_consistent_individual_types_agree() -> None:
    for name in ["fixture_hierarchy.json", "fixture_intersection_disjoint.json"]:
        items = fixture_items(name)
        _, report, verdict = _kernel_and_oracle(items)
        for ind in verdict.individuals:
            kres = report.individual(ind.individual)
            assert kres is not None and kres.conflict is None
            assert kres.types == ind.entailed_types, (name, ind.individual)


def test_subsumption_hierarchy_agrees_for_satisfiable_classes() -> None:
    items = fixture_items("fixture_hierarchy.json")
    program, report, verdict = _kernel_and_oracle(items)
    oracle_subs = set(verdict.subsumptions)
    for cls, supers in report.tbox_supers.items():
        for sup in supers:
            assert (cls, sup) in oracle_subs


def test_service_cross_check_reports_agreement_on_all_fixtures() -> None:
    svc = ReasoningService()
    for name in [
        "fixture_hierarchy.json",
        "fixture_intersection_disjoint.json",
        "fixture_mutex_instance.json",
    ]:
        items = fixture_items(name)
        program = svc.compile(items)
        report, _, _ = svc.reason(program)
        _, cross = svc.oracle_check([a for _, a in items], report)
        assert cross.agree, (name, cross.mismatches)


@pytest.mark.parametrize(
    "text",
    [
        # pure intersection reasoning
        "SubClassOf(ObjectIntersectionOf(A B) C)\nClassAssertion(ObjectIntersectionOf(A B) x)",
        # equivalence ring feeding disjointness
        "EquivalentClasses(P Q)\nDisjointClasses(Q R)\nClassAssertion(P y)\nClassAssertion(R y)",
        # deep chain
        "SubClassOf(A B)\nSubClassOf(B C)\nSubClassOf(C D)\nClassAssertion(A z)",
    ],
)
def test_arbitrary_programs_kernel_matches_oracle(text: str) -> None:
    items = functional_items(text)
    _, report, verdict = _kernel_and_oracle(items)
    assert report.inconsistent is (not verdict.ontology_consistent)
    for ind in verdict.individuals:
        kres = report.individual(ind.individual)
        assert kres is not None
        if kres.conflict is None:
            assert kres.types == ind.entailed_types
