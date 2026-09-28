"""Cross-check the engine against an independent brute-force oracle that
enumerates all assumption combinations (tests/oracle.py shares no code
with the engine)."""

from app.core.engine import ATMS
from app.core.types import Rule

from tests.oracle import oracle

# Fixed scenario with shared premises, multiple independent proofs and
# mutually exclusive assumptions. Expected values below were also worked
# out by hand (see comments) as a second anchor besides the oracle.
ASSUMPTIONS = ["A", "B", "C", "D"]
PREMISES = {"P"}
RULES = [
    {"rule_id": "r1", "antecedents": ["P", "A"], "consequent": "X"},
    {"rule_id": "r2", "antecedents": ["P", "B"], "consequent": "X"},
    {"rule_id": "r3", "antecedents": ["X", "C"], "consequent": "Y"},
    {"rule_id": "r4", "antecedents": ["D"], "consequent": "Z"},
    {"rule_id": "r5", "antecedents": ["Z", "A"], "consequent": "Y"},
    {"rule_id": "rx", "antecedents": ["C", "D"], "consequent": "⊥"},
]

# Hand-computed reference:
#   nogoods        = {{C,D}}
#   label(X)       = {{A},{B}}
#   label(Y)       = {{A,C},{B,C},{A,D}}   (env {A,C,D} etc. are nogood
#                                         supersets; {C,D}-based ones invalid)
#   label(Z)       = {{D}}
#   label(P)       = {∅}
HAND_LABELS = {
    "X": {frozenset({"A"}), frozenset({"B"})},
    "Y": {frozenset({"A", "C"}), frozenset({"B", "C"}), frozenset({"A", "D"})},
    "Z": {frozenset({"D"})},
    "P": {frozenset()},
}
HAND_NOGOODS = {frozenset({"C", "D"})}


def build_engine(budget) -> ATMS:
    engine = ATMS(budget)
    for premise in sorted(PREMISES):
        engine.add_premise(premise)
    for name in ASSUMPTIONS:
        engine.add_assumption(name)
    for spec in RULES:
        engine.add_rule(
            Rule(spec["rule_id"], tuple(spec["antecedents"]), spec["consequent"])
        )
    return engine


def test_engine_matches_hand_computed_labels(budget):
    engine = build_engine(budget)
    assert not engine.incomplete
    for node, expected in HAND_LABELS.items():
        assert engine.label(node) == expected, node
    assert engine.nogoods == HAND_NOGOODS


def test_engine_matches_oracle_on_all_nodes(budget):
    engine = build_engine(budget)
    expected = oracle(ASSUMPTIONS, PREMISES, RULES)
    assert engine.nogoods == expected["nogoods"]
    for node, envs in expected["labels"].items():
        assert engine.label(node) == envs, node


def test_engine_matches_oracle_after_retraction(budget):
    engine = build_engine(budget)
    engine.retract_assumption("C")
    remaining = [a for a in ASSUMPTIONS if a != "C"]
    expected = oracle(remaining, PREMISES, RULES)
    assert not engine.incomplete
    assert engine.nogoods == expected["nogoods"]
    for node, envs in expected["labels"].items():
        assert engine.label(node) == envs, node
