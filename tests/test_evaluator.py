"""Decision engine: deny overrides, unknown-as-deny, relevant rule set,
and order independence (exhaustive permutations)."""

from itertools import permutations

from app.interpreter import AttrBag
from app.model import DENY, PERMIT, parse_policy
from app.evaluator import (
    check_rule_order_invariance,
    evaluate_policy,
)


def rule(rid, effect, when, description=""):
    return {"id": rid, "effect": effect, "when": when, "description": description}


def lit_eq(bag_, key, value):
    return {"op": "eq",
            "left": {"op": "attr", "bag": bag_, "key": key},
            "right": {"op": "lit", "value": value}}


def policy(rid_rules, pid="p"):
    return parse_policy({"id": pid, "rules": rid_rules})


def eval(pol, subject=None, resource=None):
    return evaluate_policy(pol, AttrBag(subject), AttrBag(resource))


# --------------------------------------------------------------------- decisions
def test_matching_permit_rule_grants():
    pol = policy([rule("r1", "permit", lit_eq("subject", "role", "admin"))])
    out = eval(pol, {"role": "admin"})
    assert out["decision"] == PERMIT
    assert out["reason"] == "matched_permit"
    assert out["relevant_rules"] == ["r1"]


def test_deny_rule_overrides_permit():
    pol = policy([
        rule("allow", "allow", lit_eq("subject", "role", "admin")),
        rule("block", "deny", lit_eq("resource", "locked", True)),
    ])
    out = eval(pol, {"role": "admin"}, {"locked": True})
    assert out["decision"] == DENY
    assert out["reason"] == "explicit_deny_overrides"
    assert out["relevant_rules"] == ["block"]
    assert out["matched_permit_rules"] == ["allow"]


def test_unknown_condition_never_permits():
    # rule requires subject.clearance == 5, but clearance is absent
    pol = policy([rule("r1", "permit", lit_eq("subject", "clearance", 5))])
    out = eval(pol, {"other": 1})
    assert out["decision"] == DENY
    assert out["reason"] == "default_deny_no_match"
    assert out["relevant_rules"] == []
    trace = out["traces"][0]
    assert trace["condition"] == "unknown"
    assert trace["matched"] is False


def test_empty_policy_denies_by_default():
    pol = policy([])
    assert eval(pol)["decision"] == DENY


def test_multiple_denies_all_appear_in_relevant_set():
    pol = policy([
        rule("d1", "deny", lit_eq("subject", "a", 1)),
        rule("p1", "permit", lit_eq("subject", "b", 1)),
        rule("d2", "deny", lit_eq("resource", "c", 1)),
    ])
    out = eval(pol, {"a": 1, "b": 1}, {"c": 1})
    assert out["decision"] == DENY
    # every fired deny is part of the deciding set; permit never is
    assert out["relevant_rules"] == ["d1", "d2"]


def test_permit_relevant_set_is_all_matched_permits():
    pol = policy([
        rule("p1", "permit", lit_eq("subject", "a", 1)),
        rule("p2", "permit", lit_eq("subject", "b", 1)),
        rule("p3", "permit", lit_eq("subject", "missing", 1)),  # unknown
    ])
    out = eval(pol, {"a": 1, "b": 1})
    assert out["decision"] == PERMIT
    assert out["relevant_rules"] == ["p1", "p2"]


# --------------------------------------------------------------- order invariance
def test_result_invariant_under_all_rule_permutations():
    rules = [
        rule("d1", "deny", lit_eq("subject", "a", 1)),
        rule("d2", "deny", lit_eq("subject", "b", 1)),
        rule("p1", "permit", lit_eq("subject", "a", 1)),
        rule("p2", "permit", lit_eq("subject", "c", 1)),
        rule("u1", "permit", lit_eq("subject", "zzz", 1)),
    ]
    base = policy(rules)
    subject, resource = {"a": 1, "b": 0, "c": 1}, {}
    expected = {
        "decision": DENY,
        "matched_deny": ["d1"],
        "matched_permit": ["p1", "p2"],
    }
    fingerprints = set()
    for order in permutations(rules):
        out = eval(policy(list(order)), subject, resource)
        fingerprints.add((
            out["decision"],
            tuple(out["matched_deny_rules"]),
            tuple(out["matched_permit_rules"]),
        ))
    assert fingerprints == {(
        expected["decision"],
        tuple(expected["matched_deny"]),
        tuple(expected["matched_permit"]),
    )}
    chk = check_rule_order_invariance(base, AttrBag(subject), AttrBag(resource))
    assert chk["order_invariant"] is True
    # 5 rules -> all 120 permutations
    assert chk["variants_tested"] == 121


def test_demo_policy_scenarios(demo_policy):
    """Exercise the shipped example policy end-to-end."""
    pol = parse_policy(demo_policy)

    # owner + employee -> permit via owner (and employee rule partially)
    out = evaluate_policy(pol,
                          AttrBag({"user": "alice", "type": "employee",
                                   "verified": True, "clearance": 4}),
                          AttrBag({"owner": "alice", "action": "read",
                                   "classification": "internal",
                                   "tags": ["draft"]}))
    assert out["decision"] == PERMIT
    assert "permit-owner" in out["relevant_rules"]

    # contractor even with high clearance and owner match -> explicit deny
    out = evaluate_policy(pol,
                          AttrBag({"user": "bob", "type": "contractor",
                                   "verified": True, "clearance": 5}),
                          AttrBag({"owner": "bob", "action": "read",
                                   "classification": "internal",
                                   "tags": ["draft"]}))
    assert out["decision"] == DENY
    assert out["relevant_rules"] == ["deny-contractors"]

    # confidential tag deny wins over employee permit
    out = evaluate_policy(pol,
                          AttrBag({"user": "carol", "type": "employee",
                                   "verified": True, "clearance": 5}),
                          AttrBag({"owner": "x", "action": "read",
                                   "classification": "internal",
                                   "tags": ["confidential"]}))
    assert out["decision"] == DENY
    assert out["relevant_rules"] == ["deny-confidential-resource"]

    # public doc for a subject with almost no attributes -> permit
    out = evaluate_policy(pol,
                          AttrBag({"user": "dave"}),
                          AttrBag({"action": "read",
                                   "classification": "public"}))
    assert out["decision"] == PERMIT
    assert out["relevant_rules"] == ["permit-public-read"]

    # missing resource.classification + everything else unknown -> default deny
    out = evaluate_policy(pol,
                          AttrBag({"user": "dave"}),
                          AttrBag({"action": "read"}))
    assert out["decision"] == DENY
    assert out["reason"] == "default_deny_no_match"
