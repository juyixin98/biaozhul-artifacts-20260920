"""Parser and validator for the teaching rule language."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import FrozenSet, List, Tuple

from ..core.types import FALSE_NODE

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_MAX_LINE = 500


class RuleLanguageError(ValueError):
    """A parse/validation failure with a precise 1-based line number."""

    def __init__(self, line_no: int, message: str, line_text: str = "") -> None:
        self.line_no = line_no
        self.line_text = line_text
        super().__init__(f"line {line_no}: {message}")


@dataclass(frozen=True)
class Rule:
    rule_id: str
    antecedents: Tuple[str, ...]
    consequent: str
    line_no: int = 0

    @property
    def is_premise(self) -> bool:
        return not self.antecedents


@dataclass
class RuleSet:
    """A validated problem: assumptions, premise facts and rules."""

    assumptions: Tuple[str, ...] = ()
    facts: Tuple[str, ...] = ()
    rules: Tuple[Rule, ...] = ()

    def all_node_ids(self) -> FrozenSet[str]:
        ids: set = set(self.assumptions) | set(self.facts)
        for r in self.rules:
            ids.add(r.consequent)
            ids.update(r.antecedents)
        ids.discard(FALSE_NODE)
        return frozenset(ids)


def _check_ident(token: str, line_no: int, line_text: str, what: str) -> str:
    token = token.strip()
    if not token:
        raise RuleLanguageError(line_no, f"empty {what} identifier", line_text)
    if not _IDENT.match(token):
        raise RuleLanguageError(
            line_no, f"illegal {what} identifier {token!r}", line_text
        )
    if token == FALSE_NODE:
        raise RuleLanguageError(
            line_no, f"{what} may not use the reserved node {FALSE_NODE}", line_text
        )
    return token


def _split_csv(blob: str) -> List[str]:
    return [p.strip() for p in blob.split(",") if p.strip()]


def _starts_kw(line: str, kw: str) -> bool:
    """True if ``line`` begins with ``kw`` as a whole word."""
    if not line.startswith(kw):
        return False
    return len(line) == len(kw) or not (line[len(kw)].isalnum() or line[len(kw)] == "_")


@dataclass
class _Builder:
    assumptions: List[str] = field(default_factory=list)
    facts: List[str] = field(default_factory=list)
    rules: List[Rule] = field(default_factory=list)

    def add_assumptions(self, tokens: List[str], line_no: int, text: str) -> None:
        for tok in tokens:
            node = _check_ident(tok, line_no, text, "assumption")
            if node in self.assumptions:
                raise RuleLanguageError(line_no, f"duplicate assumption {node}", text)
            self.assumptions.append(node)

    def add_facts(self, tokens: List[str], line_no: int, text: str) -> None:
        for tok in tokens:
            node = _check_ident(tok, line_no, text, "fact")
            if node in self.facts:
                raise RuleLanguageError(line_no, f"duplicate fact {node}", text)
            self.facts.append(node)

    def add_rule(self, head: str, body: str, line_no: int, text: str) -> None:
        if "=>" not in body:
            raise RuleLanguageError(line_no, "rule needs '=>' separator", text)
        lhs, rhs = body.split("=>", 1)
        rule_id = _check_ident(head, line_no, text, "rule id")
        if any(r.rule_id == rule_id for r in self.rules):
            raise RuleLanguageError(line_no, f"duplicate rule id {rule_id}", text)
        antecedents = tuple(
            _check_ident(t, line_no, text, "antecedent") for t in _split_csv(lhs)
        )
        if not antecedents:
            raise RuleLanguageError(
                line_no, "rule needs at least one antecedent (use 'fact')", text
            )
        if len(set(antecedents)) != len(antecedents):
            raise RuleLanguageError(line_no, "repeated antecedent in rule", text)
        consequent = _split_csv(rhs)
        if len(consequent) != 1:
            raise RuleLanguageError(
                line_no, "rule must have exactly one consequent", text
            )
        c = consequent[0].strip()
        if not _IDENT.match(c):
            raise RuleLanguageError(
                line_no, f"illegal consequent identifier {c!r}", text
            )
        self.rules.append(Rule(rule_id, antecedents, c, line_no=line_no))


def parse_rules(source: str) -> RuleSet:
    """Parse DSL text into a validated :class:`RuleSet`.

    Validation performed here:
      * lexical identifier rules and reserved-word protection;
      * unique assumptions, facts, rule ids;
      * every rule has >=1 distinct antecedent and one consequent;
      * ``FALSE`` may only appear as a consequent.
    Cross-reference checks (unknown nodes) are performed by
    :func:`validate_ruleset`, which is also run at the end of parsing.
    """
    builder = _Builder()
    for line_no, raw in enumerate(source.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if len(raw) > _MAX_LINE:
            raise RuleLanguageError(line_no, f"line longer than {_MAX_LINE} chars", raw)
        text = raw.rstrip()
        if _starts_kw(line, "assume"):
            rest = line[len("assume"):].lstrip()
            builder.add_assumptions(_split_csv(rest), line_no, text)
        elif _starts_kw(line, "fact"):
            rest = line[len("fact"):].lstrip()
            builder.add_facts(_split_csv(rest), line_no, text)
        elif _starts_kw(line, "rule"):
            rest = line[len("rule"):].lstrip()
            if ":" not in rest:
                raise RuleLanguageError(
                    line_no, "rule needs 'rule <id>: <ants> => <cons>'", text
                )
            head, body = rest.split(":", 1)
            builder.add_rule(head.strip(), body.strip(), line_no, text)
        else:
            raise RuleLanguageError(
                line_no, "expected 'assume', 'fact' or 'rule'", text
            )
    rs = RuleSet(tuple(builder.assumptions), tuple(builder.facts), tuple(builder.rules))
    validate_ruleset(rs)
    return rs


def validate_ruleset(rs: RuleSet) -> None:
    """Semantic validation that needs the whole RuleSet in view."""
    declared = set(rs.assumptions) | set(rs.facts)
    produced = set(declared)
    for r in rs.rules:
        produced.add(r.consequent) if r.consequent != FALSE_NODE else None

    for r in rs.rules:
        if r.consequent == FALSE_NODE and not r.antecedents:
            raise RuleLanguageError(
                r.line_no, f"rule {r.rule_id} premises FALSE", ""
            )

    # Every antecedent must be producible somewhere.  Report one error at a
    # time with the rule that names the unknown node.
    for r in rs.rules:
        for ant in r.antecedents:
            if ant not in produced:
                raise RuleLanguageError(
                    r.line_no,
                    f"rule {r.rule_id} references unknown node {ant!r}: "
                    "declare it as assumption, fact, or rule consequent",
                )

    # Global inconsistency: the facts alone (empty assumption set) close to
    # FALSE, directly or through a chain.  Computed with a small local
    # fixpoint rather than the production kernel.
    held = set(rs.facts)
    changed = True
    while changed:
        changed = False
        for r in rs.rules:
            if r.consequent not in held and all(a in held for a in r.antecedents):
                held.add(r.consequent)
                changed = True
    if FALSE_NODE in held:
        raise RuleLanguageError(
            0,
            "facts alone derive FALSE (possibly through a chain): "
            "the theory is globally inconsistent",
        )


def serialize_ruleset(rs: RuleSet) -> str:
    """Render a RuleSet back to canonical DSL text (used by fixtures/seed)."""
    lines: List[str] = []
    if rs.assumptions:
        lines.append("assume " + ", ".join(rs.assumptions))
    for f in rs.facts:
        lines.append(f"fact {f}")
    for r in rs.rules:
        lines.append(f"rule {r.rule_id}: " + ", ".join(r.antecedents) + f" => {r.consequent}")
    return "\n".join(lines) + ("\n" if lines else "")
