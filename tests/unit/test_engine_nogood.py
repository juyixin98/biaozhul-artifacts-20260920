"""Nogood handling: contradictory environments and their supersets are
never valid support (contract item 1)."""

from app.core.engine import ATMS
from app.core.types import Rule


def test_mutually_exclusive_assumptions_form_nogood(budget):
    # A and B are mutually exclusive: together they derive ⊥.
    engine = ATMS(budget)
    engine.add_assumption("A")
    engine.add_assumption("B")
    engine.add_rule(Rule("rx", ("A", "B"), "⊥"))

    assert engine.known_nogoods() == [["A", "B"]]


def test_nogood_superset_is_not_valid_support(budget):
    # D would be derivable under {A,B}, but {A,B} is a nogood, so D must
    # have an empty label.
    engine = ATMS(budget)
    engine.add_assumption("A")
    engine.add_assumption("B")
    engine.add_rule(Rule("rx", ("A", "B"), "⊥"))
    engine.add_rule(Rule("r1", ("A", "B"), "D"))

    result = engine.query("D")
    assert result["environments"] == []
    assert result["status"] == "unsupported"  # complete, so this is definitive


def test_nogood_supersets_purged_from_existing_labels(budget):
    # Y gets {A,B} *before* the nogood is discovered; once A,B => ⊥ is
    # added, the stale support must be purged.
    engine = ATMS(budget)
    engine.add_assumption("A")
    engine.add_assumption("B")
    engine.add_rule(Rule("r1", ("A", "B"), "Y"))
    assert engine.query("Y")["environments"] == [["A", "B"]]

    engine.add_rule(Rule("rx", ("A", "B"), "⊥"))
    assert engine.known_nogoods() == [["A", "B"]]
    assert engine.query("Y")["environments"] == []


def test_superset_of_nogood_also_filtered(budget):
    # {A} alone is contradictory; {A,B} must be filtered as a superset.
    engine = ATMS(budget)
    engine.add_assumption("A")
    engine.add_assumption("B")
    engine.add_rule(Rule("rx", ("A",), "⊥"))
    engine.add_rule(Rule("r1", ("A", "B"), "Y"))

    assert engine.known_nogoods() == [["A"]]
    assert engine.query("Y")["environments"] == []


def test_nogoods_kept_subset_minimal(budget):
    engine = ATMS(budget)
    engine.add_assumption("A")
    engine.add_assumption("B")
    engine.add_rule(Rule("r1", ("A", "B"), "⊥"))
    engine.add_rule(Rule("r2", ("A",), "⊥"))
    # {A,B} is subsumed by the smaller nogood {A}.
    assert engine.known_nogoods() == [["A"]]
