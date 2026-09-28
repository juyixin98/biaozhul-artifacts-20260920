"""Cross-check the kernel against the independent brute-force oracle.

The oracle (tests/oracle.py) enumerates every assumption subset and runs
its own Horn closure; it shares no reasoning code with the kernel.  Here
we additionally assert the hand-computed label tables written in the
fixture comments, so expected answers come from both a human derivation
and an independent algorithm -- never from the kernel itself.
"""

from __future__ import annotations

import random

from atms_backend.rules.compiler import compile_ruleset
from atms_backend.rules.language import Rule, RuleSet, parse_rules
from tests.oracle import Oracle, all_subsets


def _run(source: str):
    rs = parse_rules(source)
    engine = compile_ruleset(rs)
    engine.propagate()
    oracle = Oracle(
        assumptions=list(rs.assumptions),
        facts=list(rs.facts),
        rules=[(list(r.antecedents), r.consequent) for r in rs.rules],
    )
    return rs, engine, oracle


def _kernel_label(engine, node):
    return sorted(map(sorted, engine.holding_environments(node)))


def _oracle_label(oracle, node):
    return sorted(map(sorted, oracle.label(node)))


def test_shared_reasoning_hand_computed_labels():
    source = """
    assume A, B, C
    fact P
    rule r1: A, P => X
    rule r2: B, P => X
    rule r3: X => Y
    rule r4: C => Z
    rule r5: A, C => FALSE
    """
    rs, engine, oracle = _run(source)

    # --- Hand-computed expectations (from the fixture's comment table) ---
    assert _kernel_label(engine, "P") == [[]]
    assert _kernel_label(engine, "X") == [["A"], ["B"]]
    assert _kernel_label(engine, "Y") == [["A"], ["B"]]
    assert _kernel_label(engine, "Z") == [["C"]]
    assert sorted(map(sorted, engine.nogoods)) == [["A", "C"]]

    # --- Independent enumeration agrees on EVERY node and on nogoods ---
    for node in rs.all_node_ids():
        assert _kernel_label(engine, node) == _oracle_label(oracle, node), node
    assert sorted(map(sorted, engine.nogoods)) == sorted(
        map(sorted, oracle.nogoods())
    )


def test_diamond_context_rejected_even_though_node_has_support():
    source = """
    assume A, B, D
    rule r1: A => M
    rule r2: B => M
    rule r3: M, D => Q
    rule r4: A, B => FALSE
    """
    rs, engine, oracle = _run(source)
    assert _kernel_label(engine, "M") == [["A"], ["B"]]
    assert _kernel_label(engine, "Q") == [["A", "D"], ["B", "D"]]
    assert sorted(map(sorted, engine.nogoods)) == [["A", "B"]]

    # Full context contains the nogood: rejected, naming the blocker.
    assert engine.holds("Q", frozenset({"A", "B", "D"})) is False
    assert engine.is_consistent_env(frozenset({"A", "B", "D"})) is False
    # Consistent contexts still accept.
    assert engine.holds("Q", frozenset({"A", "D"})) is True
    assert oracle.holds("Q", frozenset({"A", "B", "D"})) is False
    assert oracle.holds("Q", frozenset({"A", "D"})) is True

    for node in rs.all_node_ids():
        assert _kernel_label(engine, node) == _oracle_label(oracle, node), node


def test_exhaustive_enumeration_over_all_subsets_matches_oracle():
    # A denser theory with three nogoods and chained rules; every node's
    # label under every subset is compared with the brute-force closure.
    source = """
    assume A, B, C, D
    fact P
    rule r1: A, P => X
    rule r2: B => X
    rule r3: X, C => Y
    rule r4: Y, D => Z
    rule r5: A, B => FALSE
    rule r6: C, D => FALSE
    rule r7: A, D => FALSE
    """
    rs, engine, oracle = _run(source)
    assert not engine.incomplete
    for node in rs.all_node_ids():
        assert _kernel_label(engine, node) == _oracle_label(oracle, node), node
    assert sorted(map(sorted, engine.nogoods)) == sorted(
        map(sorted, oracle.nogoods())
    )

    # Every single subset of assumptions: holds-under-context agreement.
    assumptions = list(rs.assumptions)
    from tests.oracle import all_subsets
    for subset in all_subsets(assumptions):
        for node in rs.all_node_ids():
            assert engine.holds(node, subset) == oracle.holds(node, subset), (
                node,
                subset,
            )


def test_minimality_against_oracle_under_duplicate_derivations():
    source = """
    assume A, B, C
    rule r1: A => X
    rule r2: A, B => X
    rule r3: A, C => X
    rule r4: B, C => X
    """
    rs, engine, oracle = _run(source)
    for node in rs.all_node_ids():
        assert _kernel_label(engine, node) == _oracle_label(oracle, node), node
    # Explicit minimality: only {A} and {B,C} survive.
    assert _kernel_label(engine, "X") == [["A"], ["B", "C"]]


def _compare_table(engine, oracle, table, assumptions):
    for node in (table.nodes() + list(engine.labels.keys())):
        k = set(map(frozenset, engine.holding_environments(node)))
        o = set(table.label(node))
        assert k == o, (node, sorted(map(sorted, k)), sorted(map(sorted, o)))
    assert set(engine.nogoods) == set(table.nogoods)


def test_retraction_matches_oracle_for_withdrawn_assumptions():
    source = """
    assume A, B, C
    fact P
    rule r1: A, P => X
    rule r2: B, P => X
    rule r3: X => Y
    rule r4: C => Z
    rule r5: A, C => FALSE
    """
    rs = parse_rules(source)
    oracle = Oracle(
        assumptions=list(rs.assumptions),
        facts=list(rs.facts),
        rules=[(list(r.antecedents), r.consequent) for r in rs.rules],
    )
    for withdrawn in [("A",), ("A", "B"), ("B", "C"), ("A", "B", "C")]:
        rs2 = RuleSet(
            assumptions=tuple(a for a in rs.assumptions if a not in withdrawn),
            facts=rs.facts,
            rules=rs.rules,
        )
        engine = compile_ruleset(rs2)
        engine.propagate()
        table = oracle.withdrawn(withdrawn)
        _compare_table(engine, oracle, table, rs2.assumptions)


def test_random_theories_labels_and_contexts_agree_with_oracle():
    rng = random.Random(20260927)
    for theory_no in range(25):
        n_assumptions = rng.randint(2, 5)
        assumptions = [f"A{i}" for i in range(n_assumptions)]
        internal = [f"N{j}" for j in range(rng.randint(1, 4))]
        facts: tuple = ()
        all_nodes = assumptions + internal
        rules = []
        rid = 0
        for node in internal:
            n_ants = rng.randint(1, min(3, len(all_nodes)))
            ants = tuple(rng.sample(all_nodes, n_ants))
            rules.append(Rule(f"r{rid}", ants, node, line_no=rid + 1))
            rid += 1
        # Add zero or one contradiction.
        if rng.random() < 0.7:
            ants = tuple(rng.sample(assumptions, rng.randint(2, n_assumptions)))
            rules.append(Rule(f"r{rid}", ants, "FALSE", line_no=rid + 1))
        rs = RuleSet(assumptions=tuple(assumptions), facts=facts, rules=tuple(rules))
        oracle = Oracle(
            assumptions=list(rs.assumptions),
            facts=list(rs.facts),
            rules=[(list(r.antecedents), r.consequent) for r in rs.rules],
        )
        engine = compile_ruleset(rs)
        result = engine.propagate()
        assert result.incomplete is False, (theory_no, result.reason)
        for node in rs.all_node_ids():
            assert _kernel_label(engine, node) == _oracle_label(oracle, node), (
                theory_no, node,
            )
        assert set(engine.nogoods) == set(oracle.nogoods())
        for subset in all_subsets(assumptions):
            for node in rs.all_node_ids():
                assert engine.holds(node, subset) == oracle.holds(node, subset), (
                    theory_no, node, subset,
                )
