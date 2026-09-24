"""Multi-policy combination and explicit cross-policy conflicts."""

from itertools import permutations

from app.interpreter import AttrBag
from app.model import DENY, PERMIT, parse_policy_set
from app.evaluator import (
    check_set_order_invariance,
    evaluate_policy_set,
)


def evaluate(raw_set, subject=None, resource=None):
    policies = parse_policy_set(raw_set)
    return policies, evaluate_policy_set(
        policies, AttrBag(subject), AttrBag(resource)
    )


def test_conflict_permit_vs_explicit_deny_denies(demo_policy_set):
    # EU resource, US admin: rbac permits (admin), region explicitly denies.
    policies, out = evaluate(
        demo_policy_set,
        subject={"role": "admin", "region": "US", "groups": ["eng"]},
        resource={"group": "eng", "action": "read", "region": "EU"},
    )
    assert out["decision"] == DENY
    assert out["reason"] == "explicit_deny_overrides"
    assert out["conflict"] is True
    assert out["permit_policies"] == ["rbac-policy"]
    assert out["deny_policies"] == ["region-policy"]
    assert out["explicit_deny_policies"] == ["region-policy"]
    assert out["relevant_rules"] == ["region-policy:deny-export-us"]


def test_all_policies_must_permit(demo_policy_set):
    # Same-region group member: both policies permit, no conflict.
    _policies, out = evaluate(
        demo_policy_set,
        subject={"role": "member", "region": "EU", "groups": ["eng"]},
        resource={"group": "eng", "action": "read", "region": "EU"},
    )
    assert out["decision"] == PERMIT
    assert out["conflict"] is False
    assert out["permit_policies"] == ["rbac-policy", "region-policy"]


def test_one_default_deny_makes_set_deny(demo_policy_set):
    # Admin (rbac permit) but regions unknown on both sides -> region policy
    # defaults to deny; set deny without an "explicit" denial.
    _policies, out = evaluate(
        demo_policy_set,
        subject={"role": "admin"},
        resource={"group": "eng", "action": "read"},
    )
    assert out["decision"] == DENY
    assert out["reason"] == "default_deny_no_match"
    assert out["conflict"] is True  # one policy permits, one denies


def test_policy_set_order_invariant_under_permutations(demo_policy_set):
    policies = parse_policy_set(demo_policy_set)
    subject = {"role": "admin", "region": "US", "groups": ["eng"]}
    resource = {"group": "eng", "action": "read", "region": "EU"}
    fps = set()
    for order in permutations(policies):
        out = evaluate_policy_set(list(order), AttrBag(subject), AttrBag(resource))
        fps.add((out["decision"], tuple(sorted(out["permit_policies"])),
                 tuple(sorted(out["deny_policies"]))))
    assert len(fps) == 1
    chk = check_set_order_invariance(policies, AttrBag(subject),
                                     AttrBag(resource))
    assert chk["order_invariant"] is True
