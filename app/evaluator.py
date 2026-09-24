"""Decision engine: rule evaluation, deny-overrides combining, explanations.

Decision semantics
------------------
For one policy, rules are evaluated independently (their order is *not*
short-circuited). Each condition is three-valued:

* ``true``    — the rule *matches* and fires with its effect;
* ``false``   — the rule does not match and is irrelevant;
* ``unknown`` — a referenced attribute was missing; the rule does NOT fire.

Combining algorithm ``deny_overrides``:

* any matched ``deny`` rule  -> **deny** (explicit deny always wins);
* otherwise a matched ``permit`` rule -> **permit**;
* nothing matched (all false/unknown, or no rules) -> **deny**
  (default-deny; an unknown condition can never grant access).

``relevant_rules`` is the exact set of fired rules on the deciding side:
matched deny ids for an explicit deny, matched permit ids for a permit,
empty for a default deny. Rules whose condition was false/unknown are
excluded because they could not affect the decision. The set is sorted, so
it is identical for every rule ordering.
"""

from __future__ import annotations

from itertools import permutations
from typing import Any, Sequence

from .interpreter import AttrBag, eval_condition
from .logic import Tri
from .model import DENY, PERMIT, Policy

# Public reasons
EXPLICIT_DENY = "explicit_deny_overrides"
ALL_PERMIT = "matched_permit"
DEFAULT_DENY = "default_deny_no_match"


def evaluate_policy(
    policy: Policy,
    subject: AttrBag,
    resource: AttrBag,
) -> dict[str, Any]:
    traces: list[dict[str, Any]] = []
    matched_deny: list[str] = []
    matched_permit: list[str] = []

    for rule in policy.rules:
        tri = eval_condition(rule.when, subject, resource,
                             path=f"rule:{rule.id}")
        fired = tri is Tri.TRUE
        if fired and rule.effect == DENY:
            matched_deny.append(rule.id)
        if fired and rule.effect == PERMIT:
            matched_permit.append(rule.id)
        traces.append({
            "rule_id": rule.id,
            "effect": rule.effect,
            "condition": tri.value,
            "matched": fired,
        })

    if matched_deny:
        decision, reason = DENY, EXPLICIT_DENY
        relevant = sorted(matched_deny)
    elif matched_permit:
        decision, reason = PERMIT, ALL_PERMIT
        relevant = sorted(matched_permit)
    else:
        decision, reason = DENY, DEFAULT_DENY
        relevant = []

    return {
        "policy_id": policy.id,
        "decision": decision,
        "reason": reason,
        "matched_deny_rules": sorted(matched_deny),
        "matched_permit_rules": sorted(matched_permit),
        "relevant_rules": relevant,
        "traces": traces,
    }


def evaluate_policy_set(
    policies: Sequence[Policy],
    subject: AttrBag,
    resource: AttrBag,
) -> dict[str, Any]:
    """Evaluate several policies with deny-overrides across policies.

    *set permit* iff **every** policy permits; any policy denial makes the
    set deny. Explicit denies dominate default-denials in the explanation.
    """
    per_policy = [evaluate_policy(p, subject, resource) for p in policies]

    deny_pids = [r["policy_id"] for r in per_policy if r["decision"] == DENY]
    permit_pids = [r["policy_id"] for r in per_policy
                   if r["decision"] == PERMIT]
    explicit_deny_pids = [
        r["policy_id"] for r in per_policy if r["matched_deny_rules"]
    ]

    conflict = bool(permit_pids and deny_pids)

    if explicit_deny_pids:
        decision, reason = DENY, EXPLICIT_DENY
    elif not deny_pids:
        decision, reason = PERMIT, ALL_PERMIT
    else:
        decision, reason = DENY, DEFAULT_DENY

    # Relevant rules at the set level: fired rules in the policies that
    # decided the global outcome.
    if decision == DENY and explicit_deny_pids:
        relevant = sorted(
            f"{r['policy_id']}:{rid}"
            for r in per_policy
            if r["matched_deny_rules"]
            for rid in r["matched_deny_rules"]
        )
    elif decision == PERMIT:
        relevant = sorted(
            f"{r['policy_id']}:{rid}"
            for r in per_policy
            for rid in r["matched_permit_rules"]
        )
    else:
        relevant = []

    return {
        "decision": decision,
        "reason": reason,
        "conflict": conflict,
        "permit_policies": sorted(permit_pids),
        "deny_policies": sorted(deny_pids),
        "explicit_deny_policies": sorted(explicit_deny_pids),
        "relevant_rules": relevant,
        "policies": per_policy,
    }


# ---------------------------------------------------------------------------
# Order-independence verification
# ---------------------------------------------------------------------------

def _fingerprint(result: dict[str, Any]) -> tuple:
    """Everything that must be invariant under rule/policy reordering."""
    if "policies" in result:
        return (
            result["decision"],
            tuple(sorted(result["permit_policies"])),
            tuple(sorted(result["deny_policies"])),
            tuple(sorted(
                (p["policy_id"], tuple(p["matched_deny_rules"]),
                 tuple(p["matched_permit_rules"]))
                for p in result["policies"]
            )),
        )
    return (
        result["decision"],
        tuple(result["matched_deny_rules"]),
        tuple(result["matched_permit_rules"]),
    )


def _order_variants(seq: Sequence, *, exhaustive_max: int = 5) -> list[list]:
    """Deterministic set of orderings to test.

    Small sequences get every permutation (exhaustive proof); larger ones get
    original / ascending-by-id / descending-by-id. All orderings are chosen
    from content, never from randomness.
    """
    if len(seq) <= exhaustive_max:
        # permutations() is lexicographic over positions; dedup identical
        # sequences (duplicates do not occur — ids are unique).
        return [list(p) for p in permutations(seq)]
    ordered = list(seq)
    try:
        asc = sorted(seq, key=lambda x: x.id)
    except AttributeError:
        asc = list(seq)
    return [ordered, asc, list(reversed(asc))]


def check_rule_order_invariance(
    policy: Policy, subject: AttrBag, resource: AttrBag
) -> dict[str, Any]:
    base = evaluate_policy(policy, subject, resource)
    fp = _fingerprint(base)
    variants = _order_variants(policy.rules)
    tested = 1
    invariant = True
    for order in variants:
        tested += 1
        res = evaluate_policy(policy.with_rules(tuple(order)), subject, resource)
        if _fingerprint(res) != fp:
            invariant = False
            break
    return {
        "order_invariant": invariant,
        "variants_tested": tested,
        "fingerprint": {
            "decision": base["decision"],
            "matched_deny_rules": base["matched_deny_rules"],
            "matched_permit_rules": base["matched_permit_rules"],
        },
    }


def check_set_order_invariance(
    policies: Sequence[Policy], subject: AttrBag, resource: AttrBag
) -> dict[str, Any]:
    base = evaluate_policy_set(policies, subject, resource)
    fp = _fingerprint(base)
    tested = 1
    invariant = True
    for order in _order_variants(list(policies)):
        tested += 1
        res = evaluate_policy_set(order, subject, resource)
        if _fingerprint(res) != fp:
            invariant = False
            break

    # Also permute the rules inside every policy (content-chosen variants).
    if invariant:
        for policy in policies:
            for order in _order_variants(policy.rules):
                tested += 1
                shuffled = policy.with_rules(tuple(order))
                others = [shuffled if p.id == policy.id else p
                          for p in policies]
                res = evaluate_policy_set(others, subject, resource)
                if _fingerprint(res) != fp:
                    invariant = False
                    break
            if not invariant:
                break

    return {
        "order_invariant": invariant,
        "variants_tested": tested,
        "fingerprint": {
            "decision": base["decision"],
            "permit_policies": base["permit_policies"],
            "deny_policies": base["deny_policies"],
        },
    }
