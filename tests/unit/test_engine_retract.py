"""Retraction: removing an assumption must not delete conclusions that
still have other supporting environments (contract item 2)."""

from app.core.engine import ATMS
from app.core.types import Rule


def build_engine(budget) -> ATMS:
    engine = ATMS(budget)
    engine.add_premise("P")
    engine.add_assumption("A")
    engine.add_assumption("B")
    engine.add_rule(Rule("r1", ("P", "A"), "C"))
    engine.add_rule(Rule("r2", ("P", "B"), "C"))
    engine.add_rule(Rule("r3", ("A",), "D"))
    return engine


def test_retraction_keeps_independently_supported_conclusion(budget):
    engine = build_engine(budget)
    assert engine.query("C")["environments"] == [["A"], ["B"]]

    engine.retract_assumption("A")
    # C survives via {B}; D loses its only support.
    assert engine.query("C")["environments"] == [["B"]]
    assert engine.query("C")["status"] == "supported"
    assert engine.query("D")["environments"] == []
    assert engine.query("D")["status"] == "unsupported"


def test_retraction_removes_nogoods_that_depended_on_it(budget):
    engine = ATMS(budget)
    engine.add_assumption("A")
    engine.add_assumption("B")
    engine.add_rule(Rule("rx", ("A", "B"), "⊥"))
    assert engine.known_nogoods() == [["A", "B"]]

    engine.retract_assumption("A")
    assert engine.known_nogoods() == []


def test_retract_then_reassume_restores_labels(budget):
    engine = build_engine(budget)
    engine.retract_assumption("A")
    engine.add_assumption("A")
    assert engine.query("C")["environments"] == [["A"], ["B"]]
    assert engine.query("D")["environments"] == [["A"]]
