"""Engine tests: concrete closures, semi-naive vs naive oracle, ordering.

Expected answer sets here are computed by hand from the fixtures, *not*
read back from the engine under test.  The naive oracle
(``tests/naive_oracle.py``) is an independent implementation and serves as
the cross-check for every generated fixture.
"""

import pytest

pytestmark = pytest.mark.unit

import itertools


from conftest import load_fixture
from datalog_service.engine.fixpoint import evaluate
from datalog_service.language.compiler import compile_program
from datalog_service.language.parser import parse_program

from naive_oracle import naive_evaluate


def compile_and_evaluate(source: str):
    program = parse_program(source)
    compiled = compile_program(program)
    return compiled, evaluate(compiled, program.facts)


# --------------------------------------------------------------------------- #
# Ancestor closure: hand-computed expectations
# --------------------------------------------------------------------------- #

def test_ancestor_closure_matches_hand_computed_transitive_closure():
    compiled, mat = compile_and_evaluate(load_fixture("ancestor.dl"))
    parent = {
        ("alice", "bob"), ("bob", "carol"), ("carol", "dave"),
        ("dave", "erin"), ("alice", "frank"), ("frank", "grace"),
    }
    expected = set(parent)
    # length-2
    expected |= {
        ("alice", "carol"), ("bob", "dave"), ("carol", "erin"),
        ("alice", "grace"),
    }
    # length-3
    expected |= {("alice", "dave"), ("bob", "erin")}
    # length-4
    expected |= {("alice", "erin")}

    assert mat.relation("ancestor") == expected
    assert len(mat.relation("ancestor")) == 13
    assert mat.relation("parent") == parent


def test_ancestor_fixpoint_takes_expected_number_of_rounds():
    # Chain depth 4: full firing in round 0 derives length-1 paths,
    # productive delta rounds 1..3 add length-2..4, and a final confirming
    # round produces nothing (fixpoint reached).
    compiled, mat = compile_and_evaluate(load_fixture("ancestor.dl"))
    assert mat.strata_rounds == (3,)
    productive = [
        step.round_no
        for step in mat.trace
        if step.rule_id == "r2" and step.new_tuples > 0
    ]
    assert productive == [1, 2, 3]
    # Exactly one empty *delta* confirming round (round 0's full firing of
    # the recursive rule is empty too, because ancestor starts empty).
    empty = [
        s for s in mat.trace
        if s.rule_id == "r2" and s.new_tuples == 0 and s.variant != "full"
    ]
    assert len(empty) == 1
    assert empty[0].round_no == 4


def test_specific_ancestor_paths_have_distinct_proofs():
    compiled, mat = compile_and_evaluate(load_fixture("ancestor.dl"))
    from datalog_service.engine.derivations import build_proof_tree, leaves

    rule_texts = {cr.rule_id: cr.text for cr in compiled.rules}
    tree = build_proof_tree(
        "ancestor", ("alice", "erin"), mat.derivations, rule_texts
    )
    assert tree.kind == "derived"
    fact_leaves = [
        leaf for leaf in leaves(tree) if leaf.kind == "fact"
    ]
    assert len(fact_leaves) == 4
    predicates = [leaf.predicate for leaf in fact_leaves]
    assert predicates == ["parent"] * 4
    rows = [leaf.row for leaf in fact_leaves]
    assert rows == [
        ("alice", "bob"), ("bob", "carol"),
        ("carol", "dave"), ("dave", "erin"),
    ]


def test_base_case_tuple_is_derived_by_base_rule():
    compiled, mat = compile_and_evaluate(load_fixture("ancestor.dl"))
    derivation = mat.derivations[("ancestor", ("alice", "bob"))]
    assert derivation.rule_id == "r1"  # ancestor(X,Y) :- parent(X,Y)
    assert derivation.round_no == 0


# --------------------------------------------------------------------------- #
# Stratified negation
# --------------------------------------------------------------------------- #

def test_recursive_exclusion_removes_direct_parents():
    compiled, mat = compile_and_evaluate(load_fixture("recursive_exclusion.dl"))
    ancestor = mat.relation("ancestor")
    parent = mat.relation("parent")
    indirect = mat.relation("indirect")

    assert indirect == ancestor - parent
    assert ("alice", "bob") not in indirect
    assert ("alice", "erin") in indirect
    assert len(indirect) == 7  # 13 ancestor pairs - 6 parent pairs


def test_root_computation_through_negation():
    compiled, mat = compile_and_evaluate(load_fixture("recursive_exclusion.dl"))
    assert mat.relation("root") == {("alice",)}
    assert mat.relation("has_parent") == {
        ("bob",), ("carol",), ("dave",), ("erin",), ("frank",), ("grace",),
    }


def test_negation_is_stratified_in_separate_fixpoint():
    compiled, mat = compile_and_evaluate(load_fixture("recursive_exclusion.dl"))
    strata = {name: info.stratum for name, info in compiled.predicates.items()}
    assert strata["ancestor"] == 0
    assert strata["indirect"] == 1
    assert strata["root"] == 1
    assert mat.strata_rounds[0] >= 1  # ancestor recursion
    assert mat.strata_rounds[1] == 0  # one-shot stratum, no delta rounds


def test_unreachable_nodes_are_exactly_set_difference():
    compiled, mat = compile_and_evaluate(load_fixture("reachability.dl"))
    reachable_from_a = {
        y for (x, y) in mat.relation("reachable") if x == "a"
    }
    nodes = {row[0] for row in mat.relation("node")}
    assert mat.relation("unreachable_from_a") == {
        (n,) for n in nodes - reachable_from_a
    }
    assert ("e",) in mat.relation("unreachable_from_a")
    assert reachable_from_a == {"b", "c", "d"}


# --------------------------------------------------------------------------- #
# Semi-naive vs independent naive oracle + rule-order invariance
# --------------------------------------------------------------------------- #

def test_seminaive_matches_independent_naive_oracle_on_all_fixtures():
    for fixture in ["ancestor.dl", "recursive_exclusion.dl", "reachability.dl"]:
        source = load_fixture(fixture)
        program = parse_program(source)
        compiled = compile_program(program)
        mat = evaluate(compiled, program.facts)
        naive = naive_evaluate(compiled, program.facts)
        for predicate in compiled.predicates:
            assert mat.relation(predicate) == frozenset(
                naive.get(predicate, set())
            ), f"closure mismatch for {predicate} in {fixture}"


def test_rule_and_fact_order_permutations_produce_same_closure():
    base = parse_program(load_fixture("recursive_exclusion.dl"))

    def as_lines(program):
        return [f.canonical() + "." for f in program.facts] + [
            r.canonical() for r in program.rules
        ]

    lines = as_lines(base)
    # A spread of permutations (full factorial would be huge); include
    # reversed and interleaved orderings deterministically.
    orderings = [
        list(reversed(lines)),
        lines[::2] + lines[1::2],
        lines[1::2] + lines[::2],
        sorted(lines),
    ]
    closures = []
    versions = []
    for ordering in orderings:
        compiled, mat = compile_and_evaluate("\n".join(ordering))
        closure = {p: mat.relation(p) for p in compiled.predicates}
        closures.append(closure)
        versions.append((compiled.rule_version, mat.fact_set_version))
    reference = closures[0]
    for closure in closures[1:]:
        assert closure == reference
    assert all(v == versions[0] for v in versions)


def test_constant_terms_in_rule_body_filter_the_join():
    source = """
    edge(a, b). edge(b, c). edge(a, c). edge(c, a).
    path_from_a(Y) :- edge(a, Y).
    """
    compiled, mat = compile_and_evaluate(source)
    assert mat.relation("path_from_a") == {("b",), ("c",)}


def test_repeated_variables_must_unify():
    source = """
    eq_pairs(a, a). eq_pairs(b, b). eq_pairs(a, b).
    diagonal(X) :- eq_pairs(X, X).
    """
    compiled, mat = compile_and_evaluate(source)
    assert mat.relation("diagonal") == {("a",), ("b",)}


def test_mutual_recursion_reaches_fixpoint():
    source = """
    n(a). n(b). n(c).
    e(a, b). e(b, c).
    even(a).
    odd(Y) :- e(X, Y), even(X).
    even(Y) :- e(X, Y), odd(X).
    """
    compiled, mat = compile_and_evaluate(source)
    assert mat.relation("even") == {("a",), ("c",)}
    assert mat.relation("odd") == {("b",)}


def test_empty_extensional_predicate_yields_empty_intensional_answer():
    source = "p(X) :- q(X), r(X). q(a). q(b)."
    compiled, mat = compile_and_evaluate(source)
    assert mat.relation("p") == frozenset()


def test_no_duplicate_tuples_are_ever_derived():
    # Each delta round may re-fire rules, but absorbed duplicates are not
    # re-derived: relation sizes never count duplicates.
    compiled, mat = compile_and_evaluate(load_fixture("ancestor.dl"))
    total_new = sum(step.new_tuples for step in mat.trace)
    fact_count = len(mat.fact_rows)
    derived_total = sum(
        len(rows) for pred, rows in mat.relations.items()
    )
    assert total_new + fact_count == derived_total
