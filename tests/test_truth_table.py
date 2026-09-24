"""Enumerative truth table: row coverage, unknown cells, order checks."""

import pytest

from app.errors import PayloadError
from app.model import parse_policy, parse_policy_set
from app.truth_table import MAX_ROWS, enumerate_truth_table


ROLE_POLICY = parse_policy({
    "id": "role",
    "rules": [
        {"id": "deny-contractor", "effect": "deny",
         "when": {"op": "in",
                  "left": {"op": "attr", "bag": "subject", "key": "type"},
                  "right": {"op": "lit", "value": ["contractor"]}}},
        {"id": "permit-employee", "effect": "permit",
         "when": {"op": "eq",
                  "left": {"op": "attr", "bag": "subject", "key": "type"},
                  "right": {"op": "lit", "value": "employee"}}},
    ],
})

VARS = [
    {"bag": "subject", "key": "type",
     "values": ["employee", "contractor", "visitor"]},
]


def test_full_mode_enumerates_every_value():
    out = enumerate_truth_table(ROLE_POLICY, VARS, mode="full")
    assert out["row_count"] == 3
    decisions = {row["assignment"][0]["value"]: row["decision"]
                 for row in out["rows"]}
    assert decisions["employee"] == "permit"
    assert decisions["contractor"] == "deny"
    # visitor matches nothing -> default deny
    assert decisions["visitor"] == "deny"
    assert out["decision_counts"] == {"permit": 1, "deny": 2}
    assert out["order_invariant"] is True


def test_ternary_mode_includes_missing_cell():
    out = enumerate_truth_table(ROLE_POLICY, VARS, mode="ternary")
    states = [(row["assignment"][0]["state"]) for row in out["rows"]]
    assert states == ["value", "value", "missing"]
    missing_row = out["rows"][-1]
    # missing type -> both rules unknown -> default deny
    assert missing_row["decision"] == "deny"
    assert missing_row["reason"] == "default_deny_no_match"


def test_binary_mode_two_rows():
    out = enumerate_truth_table(ROLE_POLICY, VARS, mode="binary")
    assert out["row_count"] == 2


def test_two_variable_cartesian_product():
    vars_ = [
        {"bag": "subject", "key": "type",
         "values": ["employee", "contractor"]},
        {"bag": "resource", "key": "action",
         "values": ["read", "write"]},
    ]
    out = enumerate_truth_table(ROLE_POLICY, vars_, mode="full")
    assert out["row_count"] == 4
    assert len(out["distinct_decisions"]) >= 1


def test_truth_table_over_policy_set_reports_conflicts(demo_policy_set):
    policies = parse_policy_set(demo_policy_set)
    vars_ = [
        {"bag": "subject", "key": "region",
         "values": ["EU", "US"]},
        {"bag": "resource", "key": "region",
         "values": ["EU"]},
    ]
    # Give rbac a free permit so the EU/EU cell permits both and the EU/US
    # subject cell conflicts (rbac permit vs region deny). We achieve that
    # by ensuring subject role is provided as a fixed background attr — the
    # truth table does not support background attrs, so assert on the
    # policy set using permissive vars instead: enumerate role too.
    vars_.insert(0, {"bag": "subject", "key": "role",
                     "values": ["admin"]})
    out = enumerate_truth_table(policies, vars_, mode="full")
    assert out["row_count"] == 2
    assert out["conflicting_cells"] == 1
    assert out["order_invariant"] is True


def test_row_cap_enforced():
    vars_ = [
        {"bag": "subject", "key": f"k{i}", "values": ["a", "b"]}
        for i in range(20)
    ]
    with pytest.raises(PayloadError, match="limit"):
        enumerate_truth_table(ROLE_POLICY, vars_, mode="full")
    # sanity: 2^12=4096 allowed, 2^13=8192 rejected
    out = enumerate_truth_table(ROLE_POLICY, vars_[:12], mode="full")
    assert out["row_count"] == MAX_ROWS


def test_bad_mode_and_bad_variables():
    with pytest.raises(PayloadError):
        enumerate_truth_table(ROLE_POLICY, VARS, mode="quadratic")
    with pytest.raises(PayloadError):
        enumerate_truth_table(ROLE_POLICY, [], mode="full")
