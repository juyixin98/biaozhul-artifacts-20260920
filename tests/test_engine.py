"""Engine tests: fixpoint semantics, semi-naive vs naive equivalence,
rule-order independence, and proof-tree evidence."""

import pytest

from app.engine.fixpoint import evaluate
from app.engine.naive import naive_evaluate
from app.language.compiler import compile_program
from app.language.parser import parse_program

from .fixtures_programs import (
    ANCESTOR_EXPECTED,
    ANCESTOR_PROGRAM,
    ANCESTOR_PROGRAM_REORDERED,
    COMPARISON_PROGRAM,
    DIFF_STRATA_PROGRAM,
    FAILED_EXPECTED,
    GRAPH_PROGRAM,
    PAIR_EXPECTED,
    PASSED_EXPECTED,
    REACHABLE_EXPECTED,
    SINK2_EXPECTED,
    SINK_EXPECTED,
    UNREACHABLE_EXPECTED,
)


def eval_src(src):
    compiled = compile_program(parse_program(src))
    return compiled, evaluate(compiled)


# -- hand-computed expected answers ----------------------------------------


def test_ancestor_closure_matches_hand_computed_set():
    _, result = eval_src(ANCESTOR_PROGRAM)
    assert result.db.rels[("ancestor", 2)] == ANCESTOR_EXPECTED


def test_graph_stratified_negation_matches_hand_computed_sets():
    _, result = eval_src(GRAPH_PROGRAM)
    assert result.db.rels[("reachable", 1)] == REACHABLE_EXPECTED
    assert result.db.rels[("sink", 1)] == SINK_EXPECTED
    assert result.db.rels[("unreachable", 1)] == UNREACHABLE_EXPECTED


def test_negation_feeds_higher_stratum_rule():
    _, result = eval_src(DIFF_STRATA_PROGRAM)
    assert result.db.rels[("sink", 1)] == SINK2_EXPECTED
    assert result.db.rels[("sink_not_sink_pair", 2)] == PAIR_EXPECTED


def test_comparison_builtins():
    _, result = eval_src(COMPARISON_PROGRAM)
    assert result.db.rels[("passed", 1)] == PASSED_EXPECTED
    assert result.db.rels[("failed", 1)] == FAILED_EXPECTED


def test_set_semantics_deduplicate_facts_and_rules():
    src = (
        "p(a). p(a). p(b).\n"
        "q(X) :- p(X).\n"
        "q(X) :- p(X).\n"  # duplicate rule
        "q(a).\n"           # same tuple derivable as fact and via rule
    )
    _, result = eval_src(src)
    assert result.db.rels[("p", 1)] == {("a",), ("b",)}
    assert result.db.rels[("q", 1)] == {("a",), ("b",)}
    # q(a) given explicitly as a fact is an EDB leaf, not an IDB derivation.
    assert (("q", 1), ("a",)) in result.base_facts


# -- semi-naive vs naive oracle --------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        ANCESTOR_PROGRAM,
        ANCESTOR_PROGRAM_REORDERED,
        GRAPH_PROGRAM,
        DIFF_STRATA_PROGRAM,
        COMPARISON_PROGRAM,
        # Mutual positive recursion.
        "a(x). b(y).\np(X) :- a(X), q(X).\nq(X) :- b(X), p(X).\n",
    ],
)
def test_seminaive_closure_equals_naive_closure(src):
    compiled = compile_program(parse_program(src))
    fast = evaluate(compiled).db.rels
    slow = naive_evaluate(compiled)
    assert set(fast) == set(slow)
    for key in slow:
        assert fast[key] == slow[key], key


def test_rule_order_does_not_change_closure():
    c1 = compile_program(parse_program(ANCESTOR_PROGRAM))
    c2 = compile_program(parse_program(ANCESTOR_PROGRAM_REORDERED))
    r1 = evaluate(c1).db.rels
    r2 = evaluate(c2).db.rels
    assert r1 == r2
    assert r1[("ancestor", 2)] == ANCESTOR_EXPECTED


# -- semi-naive actually avoids redundant work -----------------------------


def test_seminaive_iterations_shrink_delta():
    compiled = compile_program(parse_program(ANCESTOR_PROGRAM))
    result = evaluate(compiled)
    stratum0 = result.traces[0]
    # 5 direct parent tuples bootstrap ancestor; then deltas 4 -> 2 -> 1,
    # shrinking each iteration as the chain closes from the leaves.
    produced = [line for line in stratum0.steps if line.startswith("iter")]
    assert "+5" in produced[0]
    assert stratum0.produced == len(ANCESTOR_EXPECTED)
    assert stratum0.iterations == 5
    # The final recorded step announces the fixpoint.
    assert "fixpoint reached" in stratum0.steps[-1]


# -- evidence --------------------------------------------------------------


def test_every_derived_tuple_has_one_canonical_firing():
    compiled, result = eval_src(ANCESTOR_PROGRAM)
    for key, relation in result.db.rels.items():
        for tup in relation:
            if (key, tup) in result.base_facts:
                continue
            firing = result.evidence.get((key, tup))
            assert firing is not None, f"missing evidence for {key}{tup}"
            # All positive support tuples actually exist.
            for supp_key, supp_tup in firing.pos_inputs:
                assert supp_tup in result.db.rels[supp_key]


def test_derived_firing_supports_head_tuple():
    compiled, result = eval_src(ANCESTOR_PROGRAM)
    firing = result.evidence[(("ancestor", 2), ("ann", "dan"))]
    # ann -> dan must be derived via the recursive rule (rule index 1),
    # since there is no direct parent(ann, dan) fact.
    assert firing.rule_index == 1
    assert firing.head == ("ann", "dan")


def test_negation_evidence_records_absence_pattern():
    compiled, result = eval_src(GRAPH_PROGRAM)
    firing = result.evidence[(("sink", 1), ("d",))]
    assert len(firing.neg_checks) == 1
    key, pattern = firing.neg_checks[0]
    assert key == ("edge", 2)
    assert pattern == ("d", "*")


def test_proof_tree_leaves_are_base_facts():
    from app.engine.proof import ProofBuilder

    compiled, result = eval_src(ANCESTOR_PROGRAM)
    texts = dict(enumerate(r.render() for r in compiled.program.rules))
    tree = ProofBuilder(result, texts, compiled.stratum_of).build(
        ("ancestor", 2), ("ann", "dan")
    ).to_dict()

    leaves = []

    def walk(node):
        if node["kind"] in ("fact", "absence"):
            leaves.append(node)
        for child in node["children"]:
            walk(child)

    walk(tree)
    assert leaves, "proof tree must terminate in EDB leaves"
    assert all(n["kind"] == "fact" for n in leaves)
    leaf_atoms = {n["atom"] for n in leaves}
    assert {"parent(ann, bob)", "parent(bob, cy)", "parent(cy, dan)"} <= leaf_atoms
