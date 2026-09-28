"""Rule compiler: validated rules -> structured matching plans.

The output is deliberately *data*, not code: each conditional element becomes
an :class:`AlphaTestPlan` describing the intra-fact tests, the variables it
introduces and the fields that identify a unique match. The Rete network in
:mod:`reteapp.core` consumes these plans; no rule semantics live there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .model import (
    Binding,
    ConditionalElement,
    LiteralConstraint,
    Rule,
    VariableConstraint,
)


class CompileError(ValueError):
    """Static semantic error in a rule set."""


@dataclass(frozen=True)
class AlphaTestPlan:
    ce_index: int
    fact_type: str
    # Structured test descriptors (dicts) so plans are JSON-serialisable for
    # the evidence store and debug traces. Each carries ``stage``:
    #   "alpha" - evaluable inside one WME (literal / first binding / same-CE var)
    #   "join"  - resolves against variables bound by earlier CEs
    tests: tuple[dict, ...]
    # variable -> source field, for bindings introduced by this CE
    binds: dict[str, str]
    # variables that must already exist in the token when this CE joins
    requires: frozenset[str]

    @property
    def alpha_tests(self) -> tuple[dict, ...]:
        return tuple(t for t in self.tests if t["stage"] == "alpha")

    def alpha_key(self) -> tuple:
        """Canonical key for sharing one alpha memory between rules/Ces."""

        encoded = tuple(sorted(
            (
                t["kind"], t["field"], t.get("op"),
                json.dumps(t.get("value"), sort_keys=True, default=str),
                t.get("variable"),
            )
            for t in self.alpha_tests
        ))
        return (self.fact_type, encoded)


@dataclass(frozen=True)
class CompiledRule:
    name: str
    salience: int
    enabled: bool
    refraction: bool
    plans: tuple[AlphaTestPlan, ...]
    action: dict  # serialised Action
    # variable -> (ce_index, field) of the binding that introduces it
    binding_origins: dict[str, tuple[int, str]]


def compile_rules(rules: list[Rule]) -> list[CompiledRule]:
    if not rules:
        raise CompileError("rule set is empty")
    seen: set[str] = set()
    compiled: list[CompiledRule] = []
    for rule in rules:
        if rule.name in seen:
            raise CompileError(f"duplicate rule name: {rule.name!r}")
        seen.add(rule.name)
        compiled.append(_compile_rule(rule))
    return compiled


def _compile_rule(rule: Rule) -> CompiledRule:
    plans: list[AlphaTestPlan] = []
    origins: dict[str, tuple[int, str]] = {}
    for ce_index, ce in enumerate(rule.conditions):
        plans.append(_compile_ce(rule.name, ce_index, ce, origins))
    _validate_action_variables(rule, origins)
    return CompiledRule(
        name=rule.name,
        salience=rule.salience,
        enabled=rule.enabled,
        refraction=rule.refraction,
        plans=tuple(plans),
        action=_serialize_action(rule),
        binding_origins=dict(origins),
    )


def _compile_ce(
    rule_name: str,
    ce_index: int,
    ce: ConditionalElement,
    origins: dict[str, tuple[int, str]],
) -> AlphaTestPlan:
    tests: list[dict] = []
    binds: dict[str, str] = {}
    requires: set[str] = set()
    for position, constraint in enumerate(ce.constraints):
        if isinstance(constraint, Binding):
            if constraint.variable in binds:
                raise CompileError(
                    f"rule {rule_name!r} CE#{ce_index}: variable ?{constraint.variable} "
                    f"bound twice in same condition"
                )
            if constraint.variable in origins:
                # Re-occurrence of an earlier variable on an equality binding
                # is a join key: test equality to the existing binding, and
                # require it in the left token.
                requires.add(constraint.variable)
                tests.append(
                    {
                        "kind": "var",
                        "stage": "join",
                        "position": position,
                        "field": constraint.field,
                        "op": "==",
                        "variable": constraint.variable,
                    }
                )
            else:
                binds[constraint.variable] = constraint.field
                tests.append(
                    {
                        "kind": "bind",
                        "stage": "alpha",
                        "position": position,
                        "field": constraint.field,
                        "variable": constraint.variable,
                    }
                )
        elif isinstance(constraint, VariableConstraint):
            if constraint.variable not in origins and constraint.variable not in binds:
                raise CompileError(
                    f"rule {rule_name!r} CE#{ce_index}: variable ?{constraint.variable} "
                    f"used before it is bound"
                )
            if constraint.variable not in binds:
                requires.add(constraint.variable)
            tests.append(
                {
                    "kind": "var",
                    # Same-CE variable references are intra-WME tests; earlier
                    # CE references are join tests.
                    "stage": "alpha" if constraint.variable in binds else "join",
                    "position": position,
                    "field": constraint.field,
                    "op": constraint.op.value,
                    "variable": constraint.variable,
                }
            )
        elif isinstance(constraint, LiteralConstraint):
            tests.append(
                {
                    "kind": "lit",
                    "stage": "alpha",
                    "position": position,
                    "field": constraint.field,
                    "op": constraint.op.value,
                    "value": constraint.value,
                }
            )
        else:  # pragma: no cover - exhaustiveness guard
            raise CompileError(f"rule {rule_name!r}: unknown constraint {constraint!r}")
    for variable, field in binds.items():
        origins[variable] = (ce_index, field)
    return AlphaTestPlan(
        ce_index=ce_index,
        fact_type=ce.type,
        tests=tuple(tests),
        binds=dict(binds),
        requires=frozenset(requires),
    )


def _validate_action_variables(rule: Rule, origins: dict[str, tuple[int, str]]) -> None:
    for template in rule.action.asserts:
        for field_name, spec in template.fields.items():
            if isinstance(spec, dict) and "variable" in spec:
                variable = spec["variable"]
                if variable not in origins:
                    raise CompileError(
                        f"rule {rule.name!r}: action asserts {template.type}.{field_name} "
                        f"from unbound variable ?{variable}"
                    )


def _serialize_action(rule: Rule) -> dict:
    return {
        "assert": [
            {"type": t.type, "fields": dict(t.fields)} for t in rule.action.asserts
        ],
        "retract_ce_indices": list(rule.action.retract_ce_indices),
        "stop": rule.action.stop,
    }
