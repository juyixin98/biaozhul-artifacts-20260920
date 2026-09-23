"""Ruleset compiler.

A ruleset document looks like::

    {
      "version": 1,
      "rules": [
        {
          "id": "phone-mask",
          "path": "$.users[*].phone",
          "transform": "mask",
          "params": {"keep_last": 4},
          "priority": 100,
          "on_missing": "ignore"
        }
      ]
    }

Default-deny validation: unknown top-level keys, unknown rule keys, unknown
transforms, unknown parameters and malformed paths all fail compilation.

Composition: every rule has an explicit integer ``priority``. At one JSON
location the highest-priority matching rule wins. Two rules with *equal*
priority that both match one location are a hard error:
statically detected when both paths are identical, dynamically (at apply
time, see :mod:`maskcompiler.engine`) when wildcards make it data-dependent.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from .errors import (
    RuleConflictError,
    RuleParameterError,
    RuleSyntaxError,
)
from .paths import parse_path
from .transforms import Transform, create_transform

RULESET_VERSION = 1
_TOP_ALLOWED = frozenset({"version", "rules"})
_RULE_ALLOWED = frozenset({"id", "path", "transform", "params", "priority", "on_missing"})


@dataclass
class RuleEntry:
    rule_id: str
    tokens: Tuple[tuple, ...]
    transform: Transform
    priority: int
    on_missing: str
    specificity: int
    match_count: int = 0


@dataclass
class CompiledRuleset:
    entries: List[RuleEntry] = field(default_factory=list)

    def bind_keys(self, bundle) -> None:
        for entry in self.entries:
            binder = getattr(entry.transform, "bind_keys", None)
            if binder is not None:
                binder(bundle)


def compile_ruleset(doc: Any) -> CompiledRuleset:
    if not isinstance(doc, dict):
        raise RuleSyntaxError("ruleset must be a JSON object")
    extra_top = sorted(set(doc) - _TOP_ALLOWED)
    if extra_top:
        raise RuleSyntaxError("unknown top-level key(s): %s" % ", ".join(extra_top))
    if doc.get("version") != RULESET_VERSION:
        raise RuleSyntaxError("missing or unsupported 'version' (supported: %d)" % RULESET_VERSION)
    rules = doc.get("rules")
    if not isinstance(rules, list) or not rules:
        raise RuleSyntaxError("'rules' must be a non-empty list")

    compiled = CompiledRuleset()
    seen_ids: Dict[str, bool] = {}
    # (priority, tokens) -> rule id, for the static equal-priority check.
    exact: Dict[Tuple[int, Tuple[tuple, ...]], str] = {}

    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise RuleSyntaxError("rule at index %d must be a JSON object" % index)
        extra = sorted(set(rule) - _RULE_ALLOWED)
        if extra:
            raise RuleSyntaxError("rule at index %d: unknown key(s) %s" % (index, ", ".join(extra)))

        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise RuleSyntaxError("rule at index %d: 'id' must be a non-empty string" % index)
        if rule_id in seen_ids:
            raise RuleSyntaxError("duplicate rule id: %s" % rule_id)
        seen_ids[rule_id] = True

        path = rule.get("path")
        if not isinstance(path, str) or not path:
            raise RuleSyntaxError("rule '%s': 'path' must be a non-empty string" % rule_id)
        tokens = tuple(parse_path(path))

        name = rule.get("transform")
        if not isinstance(name, str) or not name:
            raise RuleSyntaxError("rule '%s': 'transform' must be a non-empty string" % rule_id)
        params = rule.get("params", {})
        if not isinstance(params, dict):
            raise RuleSyntaxError("rule '%s': 'params' must be a JSON object" % rule_id)
        transform = create_transform(rule_id, name, params)

        priority = rule.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
            raise RuleParameterError("rule '%s': 'priority' must be a non-negative integer" % rule_id)

        on_missing = rule.get("on_missing", "ignore")
        if on_missing not in ("ignore", "error"):
            raise RuleParameterError("rule '%s': 'on_missing' must be 'ignore' or 'error'" % rule_id)

        key = (priority, tokens)
        if key in exact:
            raise RuleConflictError(
                "rules '%s' and '%s' have equal priority on the same path %s"
                % (exact[key], rule_id, render_path_tokens(tokens))
            )
        exact[key] = rule_id

        compiled.entries.append(
            RuleEntry(
                rule_id=rule_id,
                tokens=tokens,
                transform=transform,
                priority=priority,
                on_missing=on_missing,
                specificity=len(tokens),
            )
        )

    # Strict priority order; ties are broken deterministically by rule id so
    # per-location candidate lists always put a highest-priority rule first.
    compiled.entries.sort(key=lambda e: (-e.priority, e.rule_id))
    return compiled


def render_path_tokens(tokens: Tuple[tuple, ...]) -> str:
    # Reconstruct a readable path purely from tokens (no data involved).
    out = "$"
    for tok in tokens:
        kind = tok[0]
        if kind == "key":
            out += "." + tok[1]
        elif kind == "index":
            out += "[%d]" % tok[1]
        elif kind == "any":
            out += "[*]"
        elif kind == "rkey":
            out += ".." + tok[1]
        elif kind == "rany":
            out += "..[*]"
    return out
