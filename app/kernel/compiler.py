"""Compilation pipeline: RuleSetSpec -> validated, compiled lexer + diagnostics.

Each stage logs its key intermediate state through the run logger so a run
can be replayed from its run id:

    validate -> parse (per rule) -> nullable check -> NFA -> DFA -> diagnostics
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..config import Settings
from ..corpus.spec import RuleSetSpec
from ..errors import input_error
from .ast_nodes import count_nodes, nullable
from .dfa import DFA, determinize
from .diagnostics import Diagnostics, RuleMeta, compute_diagnostics
from .nfa import NFA
from .regex_parser import parse


class RunLog(Protocol):
    def log(self, stage: str, **detail: object) -> None: ...


@dataclass(frozen=True)
class CompiledLexer:
    rules: tuple[RuleMeta, ...]
    dfa: DFA
    diagnostics: Diagnostics


def compile_ruleset(spec: RuleSetSpec, settings: Settings, logger: RunLog) -> CompiledLexer:
    rules = tuple(
        RuleMeta(
            index=idx,
            name=rule.name,
            pattern=rule.pattern,
            priority=rule.priority if rule.priority is not None else idx,
        )
        for idx, rule in enumerate(spec.rules)
    )
    nfa = NFA(settings.max_nfa_states)
    start = nfa.new_state()
    for rule in rules:
        ast = parse(rule.pattern, max_repeat=settings.max_repeat)
        logger.log(
            "parse",
            rule=rule.name,
            pattern=rule.pattern,
            ast_nodes=count_nodes(ast),
        )
        if nullable(ast):
            raise input_error(
                f"rule {rule.name!r} can match the empty string; ordinary "
                "token rules must match at least one character",
                detail={"rule": rule.name, "pattern": rule.pattern},
            )
        frag_start, frag_end = nfa.build(ast)
        nfa.add_eps(start, frag_start)
        nfa.accepts[frag_end] = frozenset({rule.index})
    logger.log("nfa", states=nfa.state_count)
    dfa = determinize(nfa, frozenset({start}), settings.max_dfa_states)
    logger.log("dfa", states=dfa.state_count)
    diagnostics = compute_diagnostics(dfa, rules)
    logger.log(
        "diagnostics",
        overlaps=[
            {"rule_a": o.rule_a, "rule_b": o.rule_b, "witness": o.witness}
            for o in diagnostics.overlaps
        ],
        unreachable=list(diagnostics.unreachable),
        unreachable_reasons=dict(diagnostics.unreachable_reasons),
        win_witness=dict(diagnostics.win_witness),
    )
    return CompiledLexer(rules=rules, dfa=dfa, diagnostics=diagnostics)
