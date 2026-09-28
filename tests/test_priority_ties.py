"""Same-length matches are decided by explicit priority, then order."""

from __future__ import annotations

from conftest import create_ok

LIT_FIRST = [
    {"name": "LIT_AB", "pattern": "ab", "priority": 0},
    {"name": "PAIR", "pattern": "[a-c][a-c]", "priority": 1},
]
CLASS_FIRST = [
    {"name": "LIT_AB", "pattern": "ab", "priority": 1},
    {"name": "PAIR", "pattern": "[a-c][a-c]", "priority": 0},
]
DEFAULT_ORDER = [
    {"name": "LIT_AB", "pattern": "ab"},
    {"name": "PAIR", "pattern": "[a-c][a-c]"},
]


def _lex_single(client, name, rules):
    created = create_ok(client, name, rules)
    response = client.post(
        f"/api/rulesets/{created['ruleset_id']}/lex", json={"text": "ab"}
    )
    assert response.status_code == 200
    return response.json(), created


def test_explicit_priority_wins_same_length(client):
    data, _ = _lex_single(client, "tie-lit", LIT_FIRST)
    assert [(t["rule"], t["text"]) for t in data["tokens"]] == [("LIT_AB", "ab")]
    data, _ = _lex_single(client, "tie-class", CLASS_FIRST)
    assert [(t["rule"], t["text"]) for t in data["tokens"]] == [("PAIR", "ab")]


def test_declaration_order_breaks_priority_ties(client):
    data, _ = _lex_single(client, "tie-default", DEFAULT_ORDER)
    assert [(t["rule"], t["text"]) for t in data["tokens"]] == [("LIT_AB", "ab")]


def test_tie_overlap_witness(client):
    _, created = _lex_single(client, "tie-diag", LIT_FIRST)
    overlaps = created["diagnostics"]["overlaps"]
    assert overlaps == [
        {"rule_a": "LIT_AB", "rule_b": "PAIR", "witness": "ab"}
    ]
    # Both rules can still win something (PAIR wins e.g. "aa").
    assert created["diagnostics"]["unreachable"] == []
    assert created["diagnostics"]["win_witness"]["LIT_AB"] == "ab"
    assert created["diagnostics"]["win_witness"]["PAIR"] == "aa"
