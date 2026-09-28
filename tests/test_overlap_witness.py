"""Overlap witnesses are verified against an INDEPENDENT oracle.

The kernel computes the shortest common string of two patterns with its own
automata pipeline; tests/oracle.py re-derives it with Python's ``re`` engine
plus exhaustive enumeration. The two must agree exactly, and the witness must
provably lie in the intersection of the two pattern languages.
"""

from __future__ import annotations

from conftest import create_ok

from .oracle import fullmatch, verify_witness

RULES = [
    {"name": "A", "pattern": "a[bc]+d", "priority": 0},
    {"name": "B", "pattern": "[ab]c*d", "priority": 1},
    {"name": "C", "pattern": "[a-z]+", "priority": 2},
    {"name": "KW", "pattern": "if", "priority": 3},
]
PATTERNS = {rule["name"]: rule["pattern"] for rule in RULES}


def _overlaps(client):
    created = create_ok(client, "overlap-check", RULES)
    return created["diagnostics"]


def test_overlap_witnesses_match_independent_oracle(client):
    diagnostics = _overlaps(client)
    overlaps = {
        (o["rule_a"], o["rule_b"]): o["witness"] for o in diagnostics["overlaps"]
    }
    # Hand-computed reference answers.
    assert overlaps[("A", "B")] == "acd"   # a, one c, d
    assert overlaps[("A", "C")] == "abd"   # shortest A string is 3 chars
    assert overlaps[("B", "C")] == "ad"
    assert overlaps[("C", "KW")] == "if"
    # A/B and KW share nothing; A and KW share nothing.
    assert ("A", "KW") not in overlaps
    assert ("B", "KW") not in overlaps
    # Independent verification of every reported witness.
    for (rule_a, rule_b), witness in overlaps.items():
        problems = verify_witness(PATTERNS[rule_a], PATTERNS[rule_b], witness)
        assert problems == [], f"pair ({rule_a}, {rule_b}): {problems}"


def test_witness_strings_actually_lex_as_both_rules(client):
    # Sanity: the witness "acd" is accepted by both patterns under the
    # independent engine (intersection membership), and the lexer picks the
    # higher-priority rule for it.
    assert fullmatch("a[bc]+d", "acd")
    assert fullmatch("[ab]c*d", "acd")
    created = create_ok(client, "overlap-lex", RULES)
    response = client.post(
        f"/api/rulesets/{created['ruleset_id']}/lex", json={"text": "acd"}
    )
    assert [(t["rule"], t["text"]) for t in response.json()["tokens"]] == [
        ("A", "acd")
    ]


def test_unreachable_rule_reported_with_reason(client):
    diagnostics = _overlaps(client)
    # "if" is a subset of [a-z]+ and loses on priority, so KW can never win.
    assert diagnostics["unreachable"] == ["KW"]
    reason = diagnostics["unreachable_reasons"]["KW"]
    assert "KW" in reason and "never" in reason
    # The other rules each win some shortest string.
    win = diagnostics["win_witness"]
    assert set(win) == {"A", "B", "C"}
    assert fullmatch(PATTERNS["A"], win["A"])
    assert fullmatch(PATTERNS["B"], win["B"])
    assert fullmatch(PATTERNS["C"], win["C"])
