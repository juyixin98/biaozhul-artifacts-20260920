"""Independent brute-force reference matcher.

This module is the **oracle** used by the review tests. It deliberately does
NOT import anything from :mod:`reteapp.core` - no Rete network, no agenda, no
engine. The only shared code with the system under test is:

* :mod:`reteapp.lang.model`  - plain rule dataclasses (data, not behaviour)
* :mod:`reteapp.lang.predicates.evaluate` - one pure comparison function

The oracle enumerates *every* ordered WME combination per rule with
:func:`itertools.product` (a full Cartesian product, distinct WMEs only),
threads variable bindings in declared constraint order, and returns the
complete conflict set. If the indexed Rete network disagrees with this
exhaustive answer on any scenario, that is a product bug - never an oracle
bug masked by sharing the implementation.
"""

from __future__ import annotations

from itertools import product
from typing import Any

from reteapp.lang.model import (
    Binding,
    LiteralConstraint,
    Rule,
    VariableConstraint,
)
from reteapp.lang.predicates import evaluate

# A reference WME is a plain dict: {"wme_id": int, "type": str, "fields": {...}}
RefWME = dict[str, Any]
# A reference match: (rule_name, ordered wme id tuple, bindings dict)
RefMatch = tuple[str, tuple[int, ...], dict[str, Any]]

_MISSING = object()


def full_conflict_set(
    rules: list[Rule], wmes: list[RefWME]
) -> set[RefMatch]:
    """Compute every complete match for every rule by exhaustive enumeration."""

    matches: set[RefMatch] = set()
    by_type: dict[str, list[RefWME]] = {}
    for wme in wmes:
        by_type.setdefault(wme["type"], []).append(wme)

    for rule in rules:
        if not rule.enabled:
            continue
        domains: list[list[RefWME]] = []
        for ce in rule.conditions:
            domains.append(by_type.get(ce.type, []))
        if any(not domain for domain in domains):
            continue
        for combo in product(*domains):
            ids = tuple(w["wme_id"] for w in combo)
            # NOTE (documented semantics): following CLIPS/Jess, the SAME WME
            # may satisfy more than one pattern of a rule. Rules that need
            # distinct facts exclude self-matches explicitly (e.g.
            # ``id != ?other_id``). The Rete network has the same behaviour,
            # so oracle and SUT agree without special-casing.
            bindings = _evaluate_combo(rule, combo)
            if bindings is not None:
                matches.add((rule.name, ids, _freeze(bindings)))
    return matches


def _evaluate_combo(rule: Rule, combo: tuple[RefWME, ...]) -> dict[str, Any] | None:
    bindings: dict[str, Any] = {}
    for ce, wme in zip(rule.conditions, combo):
        local: dict[str, Any] = {}
        for constraint in ce.constraints:
            value = wme["fields"].get(constraint.field, _MISSING)
            if isinstance(constraint, Binding):
                if value is _MISSING:
                    return None
                if constraint.variable in bindings or constraint.variable in local:
                    if value != bindings.get(constraint.variable, local.get(constraint.variable)):
                        return None
                else:
                    local[constraint.variable] = value
            elif isinstance(constraint, VariableConstraint):
                if value is _MISSING:
                    return None
                # A variable first introduced within THIS CE is intra-WME.
                scope = local if constraint.variable in local else bindings
                if constraint.variable not in scope:
                    # Static compilation normally rejects this; the oracle is
                    # strict too instead of guessing.
                    return None
                if not evaluate(constraint.op, value, scope[constraint.variable]):
                    return None
            elif isinstance(constraint, LiteralConstraint):
                if value is _MISSING:
                    return None
                if not evaluate(constraint.op, value, constraint.value):
                    return None
        bindings.update(local)
    return bindings


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def thaw(bindings: Any) -> dict[str, Any]:
    """Reverse :func:`_freeze` for frozen binding mappings."""

    return dict(bindings) if isinstance(bindings, tuple) else bindings


def matches_by_rule(matches: set[RefMatch]) -> dict[str, set[tuple[tuple[int, ...], Any]]]:
    grouped: dict[str, set[tuple[tuple[int, ...], Any]]] = {}
    for rule_name, ids, bindings in matches:
        grouped.setdefault(rule_name, set()).add((ids, bindings))
    return grouped


def diff_conflict_sets(
    expected: set[RefMatch], actual: set[RefMatch]
) -> dict[str, list[RefMatch]]:
    """Return ``missing`` (in oracle, not rete) and ``extra`` (rete only)."""

    return {
        "missing": sorted(expected - actual, key=lambda m: (m[0], m[1])),
        "extra": sorted(actual - expected, key=lambda m: (m[0], m[1])),
    }
