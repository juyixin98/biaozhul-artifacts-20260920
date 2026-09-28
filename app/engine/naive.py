"""Naive fixpoint evaluation — the *independent* reference implementation.

Unlike :mod:`app.engine.fixpoint` (semi-naive, delta-driven, evidence
recording), this evaluator is deliberately the simplest plausible thing:

* every literal of every rule is always scanned against the **full**
  relation;
* all rules are re-run from scratch on every iteration;
* no firings or evidence are retained, only the resulting relation sets.

It shares only the parser/compiler (stratification) and the single-literal
matching primitive.  Its purpose is to be an oracle in tests: the
semi-naive engine, the naive engine, and semi-naive over permuted rule
order must all yield identical closures.
"""

from __future__ import annotations

from ..language.compiler import WILDCARD_PREFIX, CompiledProgram
from ..language.terms import Atom, Comparison, Const, NegAtom, Rule, Term, Var
from .relations import PredKey, _any_matching, _as_number

GROUND = tuple[str, ...]


def _bind(args: tuple[Term, ...], tup: GROUND, env: dict[str, str]) -> dict[str, str] | None:
    out = dict(env)
    for term, value in zip(args, tup):
        if isinstance(term, Const):
            if term.value != value:
                return None
        else:
            assert isinstance(term, Var)
            if term.name in out and out[term.name] != value:
                return None
            out[term.name] = value
    return out


def _value(term: Term, env: dict[str, str]) -> str | None:
    return term.value if isinstance(term, Const) else env.get(term.name)


def _cmp_holds(cmp: Comparison, env: dict[str, str]) -> bool:
    lv, rv = _value(cmp.left, env), _value(cmp.right, env)
    if lv is None or rv is None:
        return False
    if cmp.op == "=":
        return lv == rv
    if cmp.op == "!=":
        return lv != rv
    ln, rn = _as_number(lv), _as_number(rv)
    a: object = ln if ln is not None else lv
    b: object = rn if rn is not None else rv
    return {
        "<": a < b,
        "<=": a <= b,
        ">": a > b,
        ">=": a >= b,
    }[cmp.op]


def _match_rule(rule: Rule, rels: dict[PredKey, set[GROUND]]) -> set[GROUND]:
    positives = [lit for lit in rule.body if isinstance(lit, Atom)]
    negatives = [lit for lit in rule.body if isinstance(lit, NegAtom)]
    comparisons = [lit for lit in rule.body if isinstance(lit, Comparison)]
    results: set[GROUND] = set()

    def project(env: dict[str, str]) -> GROUND:
        return tuple(_value(a, env) or "" for a in rule.head.args)

    def scan(k: int, env: dict[str, str]) -> None:
        if k == len(positives):
            if not all(_cmp_holds(c, env) for c in comparisons):
                return
            for neg in negatives:
                key = (neg.atom.pred, neg.atom.arity)
                pattern = tuple(
                    "*"
                    if isinstance(a, Var) and a.name.startswith(WILDCARD_PREFIX)
                    else (_value(a, env) or "")
                    for a in neg.atom.args
                )
                if _any_matching(rels.get(key, set()), pattern):
                    return
            results.add(project(env))
            return
        atom = positives[k]
        for tup in rels.get((atom.pred, atom.arity), set()):
            extended = _bind(atom.args, tup, env)
            if extended is not None:
                scan(k + 1, extended)

    scan(0, {})
    return results


def naive_evaluate(compiled: CompiledProgram) -> dict[PredKey, set[GROUND]]:
    rels: dict[PredKey, set[GROUND]] = {}
    for fact in compiled.program.facts:
        rels.setdefault((fact.pred, fact.arity), set()).add(
            tuple(a.value for a in fact.args)  # type: ignore[union-attr]
        )

    for level, rule_indices in enumerate(compiled.rules_by_stratum):
        if not rule_indices:
            continue
        rules = [compiled.program.rules[i] for i in rule_indices]
        for p in compiled.strata[level]:
            rels.setdefault(p, set())
        while True:
            added: dict[PredKey, set[GROUND]] = {}
            for rule in rules:
                key = (rule.head.pred, rule.head.arity)
                for tup in _match_rule(rule, rels):
                    if tup not in rels[key]:
                        added.setdefault(key, set()).add(tup)
            if not added:
                break
            for key, tuples in added.items():
                rels[key] |= tuples
    return rels
