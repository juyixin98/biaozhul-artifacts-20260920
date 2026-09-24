"""Enumerative truth-table generation over attribute assignments.

Given a small set of *variables* (bag/key plus candidate values) the engine
evaluates the policy for every Cartesian-product assignment. This is the
acceptance tool for "enumerate combined truth values": the caller can see
every true/false/unknown outcome, the resulting decision, and the cells
where permit/deny policies conflict.

Bounds keep the endpoint offline-friendly:

* :data:`MAX_ROWS` combinations per request (4096);
* order-independence is re-checked on a deterministic sample of up to
  :data:`ORDER_SAMPLE` cells (full permutations of every policy's rules per
  sampled cell), so the cost never grows with the row count.
"""

from __future__ import annotations

from itertools import product
from typing import Any, Sequence

from .errors import PayloadError
from .evaluator import (
    check_rule_order_invariance,
    check_set_order_invariance,
    evaluate_policy,
    evaluate_policy_set,
)
from .interpreter import BAGS, AttrBag, is_literal
from .model import Policy

MAX_ROWS = 4096
ORDER_SAMPLE = 64

VALID_MODES = ("binary", "ternary", "full")


def _parse_variables(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise PayloadError("'variables' must be a non-empty list")
    variables: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for i, v in enumerate(raw):
        path = f"variables[{i}]"
        if not isinstance(v, dict):
            raise PayloadError(f"{path} must be an object")
        bag = v.get("bag")
        if bag not in BAGS:
            raise PayloadError(f"{path}.bag must be one of {list(BAGS)}")
        key = v.get("key")
        if not isinstance(key, str) or not key:
            raise PayloadError(f"{path}.key must be a non-empty string")
        values = v.get("values")
        if not isinstance(values, list) or not values:
            raise PayloadError(f"{path}.values must be a non-empty list")
        for j, val in enumerate(values):
            if not is_literal(val):
                raise PayloadError(
                    f"{path}.values[{j}] must be a JSON scalar or scalar list"
                )
        ident = (bag, key)
        if ident in seen:
            raise PayloadError(f"duplicate variable {bag}.{key}")
        seen.add(ident)
        extra = set(v) - {"bag", "key", "values"}
        if extra:
            raise PayloadError(f"{path} has unexpected field(s) {sorted(extra)}")
        variables.append({"bag": bag, "key": key, "values": values})
    return variables


def _domains(variables: list[dict[str, Any]], mode: str,
             include_missing: bool) -> list[list[dict[str, Any]]]:
    domains: list[list[dict[str, Any]]] = []
    for var in variables:
        if mode == "binary":
            if len(var["values"]) < 2:
                raise PayloadError(
                    f"variable {var['bag']}.{var['key']} needs >= 2 values "
                    "for binary mode"
                )
            cells = [{"state": "value", "value": v} for v in var["values"][:2]]
        elif mode == "ternary":
            if len(var["values"]) < 2:
                raise PayloadError(
                    f"variable {var['bag']}.{var['key']} needs >= 2 values "
                    "for ternary mode"
                )
            cells = (
                [{"state": "value", "value": v} for v in var["values"][:2]]
                + [{"state": "missing"}]
            )
        else:  # full
            cells = [{"state": "value", "value": v} for v in var["values"]]
            if include_missing:
                cells.append({"state": "missing"})
        domains.append(cells)
    return domains


def _bags_from_assignment(variables, combo) -> tuple[AttrBag, AttrBag]:
    subj: dict[str, Any] = {}
    res: dict[str, Any] = {}
    assignment = []
    for var, cell in zip(variables, combo):
        entry = {"bag": var["bag"], "key": var["key"], "state": cell["state"]}
        if cell["state"] == "value":
            entry["value"] = cell["value"]
            (subj if var["bag"] == "subject" else res)[var["key"]] = cell["value"]
        assignment.append(entry)
    return AttrBag(subj), AttrBag(res), assignment


def _sample_indices(n: int, cap: int = ORDER_SAMPLE) -> list[int]:
    if n <= cap:
        return list(range(n))
    step = (n - 1) / (cap - 1)
    return sorted({int(round(i * step)) for i in range(cap)})


def enumerate_truth_table(
    policy_or_set: Policy | Sequence[Policy],
    raw_variables: Any,
    *,
    mode: str = "ternary",
    include_missing: bool = False,
) -> dict[str, Any]:
    if mode not in VALID_MODES:
        raise PayloadError(f"'mode' must be one of {list(VALID_MODES)}")
    variables = _parse_variables(raw_variables)
    domains = _domains(variables, mode, include_missing)

    combos = list(product(*domains))
    if len(combos) > MAX_ROWS:
        raise PayloadError(
            f"truth table would have {len(combos)} rows; limit is {MAX_ROWS}"
        )

    is_set = not isinstance(policy_or_set, Policy)
    rows: list[dict[str, Any]] = []
    decision_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    conflicts = 0

    for idx, combo in enumerate(combos):
        subject, resource, assignment = _bags_from_assignment(variables, combo)
        if is_set:
            result = evaluate_policy_set(policy_or_set, subject, resource)
            row = {
                "n": idx,
                "assignment": assignment,
                "decision": result["decision"],
                "reason": result["reason"],
                "conflict": result["conflict"],
                "relevant_rules": result["relevant_rules"],
            }
            conflicts += int(result["conflict"])
        else:
            result = evaluate_policy(policy_or_set, subject, resource)
            row = {
                "n": idx,
                "assignment": assignment,
                "decision": result["decision"],
                "reason": result["reason"],
                "relevant_rules": result["relevant_rules"],
            }
        rows.append(row)
        decision_counts[row["decision"]] = decision_counts.get(row["decision"], 0) + 1
        reason_counts[row["reason"]] = reason_counts.get(row["reason"], 0) + 1

    # Deterministic order-independence re-check on a bounded cell sample.
    sample = _sample_indices(len(rows))
    invariant = True
    for idx in sample:
        subject, resource, _ = _bags_from_assignment(variables, combos[idx])
        if is_set:
            chk = check_set_order_invariance(policy_or_set, subject, resource)
        else:
            chk = check_rule_order_invariance(policy_or_set, subject, resource)
        if not chk["order_invariant"]:
            invariant = False
            break

    return {
        "mode": mode,
        "variables": [
            {"bag": v["bag"], "key": v["key"], "domain_size": len(d)}
            for v, d in zip(variables, domains)
        ],
        "row_count": len(rows),
        "decision_counts": decision_counts,
        "reason_counts": reason_counts,
        "distinct_decisions": sorted(decision_counts),
        "conflicting_cells": conflicts if is_set else None,
        "order_invariant": invariant,
        "order_sample_cells": len(sample),
        "rows": rows,
    }
