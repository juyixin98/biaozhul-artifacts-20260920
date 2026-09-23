"""Apply a compiled ruleset to a JSON document.

Semantics
---------
* All path matching is done against the **original** document before anything
  is transformed, so a high-priority ``redact`` cannot erase locations a
  lower-priority rule was meant to see.
* Each directly matched location keeps its single highest-priority rule. If
  two rules of the *same* maximum priority cover one location (only possible
  through wildcards once compile-time duplicates are ruled out),
  :class:`RuleConflictError` is raised.
* A container-covering transform (``redact``) replaces its whole subtree. A
  descendant with a higher-priority direct rule is carved out of the
  redaction; a descendant with an equal-priority *different* rule is a
  conflict; lower-priority descendant rules are swallowed by the redaction.
* Fail closed: a scalar transform (mask/hash/encrypt) meeting a non-string
  JSON value raises :class:`TypeMismatchError`; ``null`` passes through.
* ``on_missing: error`` raises :class:`MissingFieldError` when a rule matches
  zero locations.
* The input document is never mutated.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .compiler import CompiledRuleset, RuleEntry, render_path_tokens
from .errors import (
    MissingFieldError,
    RuleConflictError,
    TypeMismatchError,
)
from .logsafe import register_secret, secret_scope
from .paths import Location, match, render_location


@dataclass
class ApplyReport:
    transformed_locations: int = 0
    matched_rules: Dict[str, int] = field(default_factory=dict)


@dataclass
class MaskResult:
    output: Any
    report: ApplyReport


def apply_ruleset(compiled: CompiledRuleset, document: Any) -> MaskResult:
    with secret_scope():
        # Register every scalar in the input with the log-redaction scope so
        # that no original value can appear in a log line even on error.
        _register_all(document)
        return _run(compiled, document)


def _run(compiled: CompiledRuleset, document: Any) -> MaskResult:
    # 1) Match every rule against the original document (priority order).
    raw_matches: List[Tuple[RuleEntry, int]] = []
    by_location: Dict[Location, List[RuleEntry]] = {}
    for entry in compiled.entries:
        found = match(list(entry.tokens), document)
        entry.match_count = len(found)
        raw_matches.append((entry, len(found)))
        for loc, _value in found:
            cands = by_location.setdefault(loc, [])
            if not any(e is entry for e in cands):
                cands.append(entry)

    # 2) Missing-field policy.
    for entry, count in raw_matches:
        if entry.on_missing == "error" and count == 0:
            raise MissingFieldError(
                "rule '%s' on path %s matched no field"
                % (entry.rule_id, render_path_tokens(entry.tokens))
            )

    report = ApplyReport()
    for entry, count in raw_matches:
        report.matched_rules[entry.rule_id] = count

    def winner_at(loc: Location) -> Optional[RuleEntry]:
        cands = by_location.get(loc)
        if not cands:
            return None
        top = cands[0].priority
        tied = [e for e in cands if e.priority == top]
        if len(tied) > 1:
            raise RuleConflictError(
                "rules %s tie at priority %d at path %s"
                % (
                    ", ".join("'%s'" % e.rule_id for e in tied),
                    top,
                    render_location(loc),
                )
            )
        return tied[0]

    def apply_scalar_winner(entry: RuleEntry, node: Any, loc: Location) -> Any:
        if node is None:
            return None  # null carries no value; leave it untouched
        if not isinstance(node, str):
            raise TypeMismatchError(
                "rule '%s' at path %s expects a string, got %s"
                % (entry.rule_id, render_location(loc), _type_name(node))
            )
        report.transformed_locations += 1
        return entry.transform.apply_scalar(node)

    def apply_covering(entry: RuleEntry, node: Any, loc: Location) -> Any:
        """Apply a container-covering rule, carving out stronger descendants."""
        if not isinstance(node, (dict, list)):
            report.transformed_locations += 1
            return entry.transform.apply_node(node)

        built: Any = {} if isinstance(node, dict) else []
        iterable = node.items() if isinstance(node, dict) else enumerate(node)
        for step, child in iterable:
            child_loc = loc + (step,)
            child_winner = winner_at(child_loc)
            if (
                child_winner is not None
                and child_winner is not entry
                and child_winner.priority == entry.priority
            ):
                raise RuleConflictError(
                    "rules '%s' and '%s' tie at priority %d at path %s"
                    % (
                        entry.rule_id,
                        child_winner.rule_id,
                        entry.priority,
                        render_location(child_loc),
                    )
                )
            if child_winner is not None and child_winner.priority > entry.priority:
                # A stronger rule carves this child out of the subtree; it
                # counts itself when it transforms something.
                resolved = walk(child, child_loc)
            elif child_winner is entry and isinstance(child, (dict, list)):
                # The same covering rule also matched the child directly
                # (recursive patterns): recurse to preserve its carve-outs.
                resolved = apply_covering(entry, child, child_loc)
            else:
                # No direct rule or a weaker one: the whole fragment is
                # replaced by the covering rule's replacement value.
                resolved = entry.transform.apply_node(child)
            if isinstance(built, dict):
                built[step] = resolved
            else:
                built.append(resolved)
        report.transformed_locations += 1
        return built

    def walk(node: Any, loc: Location) -> Any:
        winner = winner_at(loc)
        if winner is None:
            if isinstance(node, dict):
                return {k: walk(v, loc + (k,)) for k, v in node.items()}
            if isinstance(node, list):
                return [walk(v, loc + (i,)) for i, v in enumerate(node)]
            return node
        if winner.transform.handles_containers:
            mode = getattr(winner.transform, "container_mode", "recurse")
            if mode == "replace":
                # Whole matched fragment replaced, regardless of descendants.
                report.transformed_locations += 1
                return winner.transform.apply_node(node)
            return apply_covering(winner, node, loc)
        if isinstance(node, (dict, list)):
            raise TypeMismatchError(
                "rule '%s' at path %s expects a string, got %s"
                % (winner.rule_id, render_location(loc), _type_name(node))
            )
        return apply_scalar_winner(winner, node, loc)

    output = walk(document, ())
    return MaskResult(output=output, report=report)


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _register_all(node: Any) -> None:
    if isinstance(node, str):
        register_secret(node)
    elif isinstance(node, bool):
        register_secret(str(node))
    elif isinstance(node, (int, float)):
        register_secret(str(node))
    elif isinstance(node, dict):
        for v in node.values():
            _register_all(v)
    elif isinstance(node, list):
        for v in node:
            _register_all(v)
